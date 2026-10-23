import os
import secrets
from sqlalchemy import update, select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.concurrency import run_in_threadpool
from datetime import timedelta, timezone, datetime
from fastapi import APIRouter, Depends, HTTPException, Request, Response, Request

from services.rate_limiter import limiter
from database import User, get_db, RefreshToken
from services.utils import get_logger, report_and_raise_500
from schema import SignupForm, LoginForm, VerifyOTP, RequestPasswordReset
from services.auth import (
    create_access_token,
    verify_password,
    hash_password,
    store_refresh_token_in_db,
    generate_otp,
    send_otp_email,
    reset_serializer,
)

router = APIRouter()

logger = get_logger(__file__)
TIMEDELTA = int(os.environ.get("TIMEDELTA", "15"))


@router.post("/api/auth/signup")
@limiter.limit("75/minute")
async def signup(
    signup_form: SignupForm, request: Request, db: AsyncSession = Depends(get_db)
):
    try:
        stmt = select(User).where(User.email == signup_form.email)
        result = await db.execute(stmt)
        existing_user = result.scalar_one_or_none()
        if existing_user:
            raise HTTPException(status_code=400, detail="Username already exists")

        otp = generate_otp()
        expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10)
        hashed_password = await run_in_threadpool(hash_password, signup_form.password)

        new_user = User(
            email=signup_form.email,
            password=hashed_password,
            name=signup_form.name,
            otp_code=otp,
            otp_expires_at=expiry,
            tokens=1,
        )
        db.add(new_user)
        await db.commit()

        await send_otp_email(signup_form.email, otp)

        return {"status_code": 200, "data": "User Created Successfully!"}

    except HTTPException as ht:
        if ht.detail in [
            "Username already exists",
        ]:
            raise
        else:
            await report_and_raise_500(ht, context="login: HTTP", raise_error=False)
            raise

    except Exception as e:
        await report_and_raise_500(e, context="signup: Exception")


@router.post("/api/auth/login")
async def login(
    login_form: LoginForm, response: Response, db: AsyncSession = Depends(get_db)
):
    try:
        stmt = select(User).where(User.email == login_form.email)
        result = await db.execute(stmt)
        user = result.scalar_one_or_none()

        if not user:
            raise HTTPException(status_code=401, detail="Invalid credentials.")

        if not user.is_verified:
            otp = generate_otp()
            expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
                minutes=10
            )

            update_stmt = (
                update(User)
                .where(User.email == user.email)
                .values(otp_code=otp, otp_expires_at=expiry)
            )
            await db.execute(update_stmt)
            await db.commit()

            await send_otp_email(str(user.email), otp)
            raise HTTPException(status_code=400, detail="Email not verified.")

        valid_pwd = await run_in_threadpool(
            verify_password, login_form.password, str(user.password)
        )
        if not valid_pwd:
            raise HTTPException(status_code=401, detail="Invalid credentials.")

        access_token = create_access_token(
            data={"sub": user.email}, expires_delta=timedelta(minutes=TIMEDELTA)
        )
        refresh_token = secrets.token_urlsafe(64)

        cookie_max_age = 30 * 24 * 60 * 60
        is_prod = os.environ.get("ENVIRONMENT", "production") == "production"
        response.set_cookie(
            key="refresh_token",
            value=refresh_token,
            httponly=True,
            secure=is_prod,  # allow non-HTTPS only in local dev
            samesite="lax",
            max_age=cookie_max_age,
        )
        await store_refresh_token_in_db(
            db, user_email=str(user.email), token_string=refresh_token
        )

        return {
            "status_code": 200,
            "access_token": access_token,
            "token_type": "bearer",
        }

    except HTTPException as ht:
        if ht.detail in [
            "Invalid credentials.",
            "Verification code has expired",
            "Email not verified.",
        ]:
            raise
        else:
            await report_and_raise_500(ht, context="login: HTTP", raise_error=False)
            raise

    except Exception as e:
        await report_and_raise_500(e, context="login: Exception")


@router.post("/api/auth/logout")
async def logout(
    request: Request, response: Response, db: AsyncSession = Depends(get_db)
):
    try:
        refresh_token = request.cookies.get("refresh_token")
        if not refresh_token:
            return {"status_code": 200, "data": "Cookie Deleted successfully."}

        stmt = select(RefreshToken).where(
            RefreshToken.token == refresh_token, RefreshToken.revoked == False
        )
        result = await db.execute(stmt)
        db_token = result.scalar_one_or_none()

        if not db_token or db_token.expires_at < datetime.utcnow():
            raise HTTPException(
                status_code=401, detail="Invalid or expired refresh token"
            )

        update_stmt = (
            update(RefreshToken)
            .where(RefreshToken.token == refresh_token)
            .values(revoked=True)
        )
        await db.execute(update_stmt)
        await db.commit()

        response.delete_cookie("refresh_token")
        return {"status_code": 200, "data": "Cookie Deleted successfully."}

    except HTTPException as ht:
        if ht.detail in ["Invalid or expired refresh token"]:
            raise
        else:
            await report_and_raise_500(
                ht, context="verify_email: HTTP", raise_error=False
            )
            raise

    except Exception as e:
        await report_and_raise_500(e, context="logout: Exception")


@router.post("/api/auth/verify-email")
@limiter.limit("75/minute")
async def verify_email(
    payload: VerifyOTP, request: Request, db: AsyncSession = Depends(get_db)
):
    try:
        # 1. Fetch user by email
        result = await db.execute(select(User).where(User.email == payload.email))
        user = result.scalars().first()

        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if user.is_verified:
            return {"status_code": 200, "data": "Email is already verified."}

        # 2. Validate OTP and Expiration
        current_time = datetime.now(timezone.utc).replace(tzinfo=None)

        if user.otp_code != payload.otp:
            raise HTTPException(status_code=400, detail="Invalid verification code")

        if user.otp_expires_at < current_time:
            raise HTTPException(status_code=400, detail="Verification code has expired")

        # 3. Mark user as verified and clean up OTP fields
        user.is_verified = True  # type: ignore[assignment]
        user.otp_code = None  # type: ignore[assignment]
        user.otp_expires_at = None  # type: ignore[assignment]
        await db.commit()

        return {
            "status_code": 200,
            "data": "Email verified successfully! You can now log in.",
        }

    except HTTPException as ht:
        if ht.detail in [
            "User not found",
            "Verification code has expired",
            "Invalid verification code",
        ]:
            raise
        else:
            await report_and_raise_500(
                ht, context="verify_email: HTTP", raise_error=False
            )
            raise

    except Exception as e:
        await report_and_raise_500(e, context="verify_email: Exception")


# Refresh Token Endpoint


@router.post("/api/auth/refresh")
@limiter.limit("1000/minute")
async def refresh_access_token(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        # Read the refresh token from the HttpOnly cookie
        refresh_token = request.cookies.get("refresh_token")
        if not refresh_token:
            raise HTTPException(status_code=401, detail="Refresh token missing")

        # Check the database
        stmt = select(RefreshToken).where(
            RefreshToken.token == refresh_token, RefreshToken.revoked == False
        )
        result = await db.execute(stmt)
        db_token = result.scalar_one_or_none()

        if not db_token or db_token.expires_at < datetime.utcnow():
            raise HTTPException(
                status_code=401, detail="Invalid or expired refresh token"
            )

        # Issue a new Access Token
        new_access_token = create_access_token(
            data={"sub": db_token.user_email},
            expires_delta=timedelta(minutes=TIMEDELTA),
        )

        return {
            "status_code": 200,
            "access_token": new_access_token,
            "token_type": "bearer",
        }
    except HTTPException as ht:
        if ht.detail in [
            "Refresh token missing",
            "Invalid or expired refresh token",
        ]:
            raise
        else:
            await report_and_raise_500(
                ht, context="refresh_access_token: HTTP", raise_error=False
            )
            raise

    except Exception as e:
        await report_and_raise_500(e, context="refresh_access_token: Exception")


# Forgot Password Endpoint


@router.post("/api/auth/request-password-reset")
async def request_password_reset(
    payload: RequestPasswordReset, db: AsyncSession = Depends(get_db)
):
    try:
        result = await db.execute(select(User).where(User.email == payload.email))
        user = result.scalars().first()

        # Don't reveal whether the email exists — same generic response either way
        if not user:
            return {
                "status_code": 200,
                "data": "If an account exists for this email, a reset code has been sent.",
            }

        user.reset_otp_code = generate_otp()  # type: ignore[assignment]
        print(user.reset_otp_code)
        user.reset_otp_expires_at = datetime.now(timezone.utc).replace(  # type: ignore[assignment]
            tzinfo=None
        ) + timedelta(
            minutes=10
        )
        await db.commit()

        await send_otp_email(
            str(user.email), str(user.reset_otp_code)
        )  # reuse your existing sender

        return {
            "status_code": 200,
            "data": "If an account exists for this email, a reset code has been sent.",
        }

    except HTTPException as ht:
        await report_and_raise_500(
            ht, context="request_password_reset: HTTP", raise_error=False
        )
        raise

    except Exception as e:
        await report_and_raise_500(e, context="request_password_reset: Exception")


@router.post("/api/auth/verify-reset-otp")
async def verify_reset_otp(
    payload: VerifyOTP, response: Response, db: AsyncSession = Depends(get_db)
):
    try:
        result = await db.execute(select(User).where(User.email == payload.email))
        user = result.scalars().first()

        if not user:
            raise HTTPException(status_code=400, detail="Invalid or expired code")

        current_time = datetime.now(timezone.utc).replace(tzinfo=None)

        if not user.reset_otp_code or user.reset_otp_code != payload.otp:
            raise HTTPException(status_code=400, detail="Invalid verification code")

        if not user.reset_otp_expires_at or user.reset_otp_expires_at < current_time:
            raise HTTPException(status_code=400, detail="Verification code has expired")

        # single-use
        user.reset_otp_code = None  # type: ignore[assignment]
        user.reset_otp_expires_at = None  # type: ignore[assignment]
        await db.commit()

        token = reset_serializer.dumps(
            user.email, salt=os.environ.get("RESET_TOKEN_SALT")
        )

        is_prod = os.environ.get("ENVIRONMENT", "production") == "production"
        response.set_cookie(
            key="reset_token",
            value=token,
            httponly=True,
            secure=is_prod,  # see dev note below
            samesite="lax",
            max_age=15 * 60,
        )

        return {"status_code": 200, "data": "Code verified."}

    except HTTPException as ht:
        if ht.detail in [
            "Invalid or expired code",
            "Invalid verification code",
            "Verification code has expired",
        ]:
            raise
        else:
            await report_and_raise_500(
                ht, context="verify_reset_otp: HTTP", raise_error=False
            )
            raise

    except Exception as e:
        await report_and_raise_500(e, context="verify_reset_otp: Exception")
