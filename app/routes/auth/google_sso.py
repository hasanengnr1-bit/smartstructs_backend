import os
import secrets
from datetime import timedelta, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from authlib.integrations.starlette_client import OAuth, OAuthError  # type: ignore[import-untyped]
from fastapi import APIRouter, Request, Depends, Response, HTTPException
from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from database import (
    get_db,
    User,
    OAuthExchangeCode,
)  # OAuthExchangeCode: new model, see below
from services.utils import get_logger, report_and_raise_500
from services.auth import create_access_token, store_refresh_token_in_db, hash_password

router = APIRouter()
oauth = OAuth()
logger = get_logger(__file__)

# --- Config -----------------------------------------------------------------

SECRET_KEY = os.environ[
    "SERIALIZER_SECRET_KEY"
]  # fail loudly if missing; this signs OAuth state
FRONTEND_URL = os.environ["FRONTEND_URL"].rstrip(
    "/"
)  # e.g. https://app.smartstructs.org
TIMEDELTA = int(os.environ.get("TIMEDELTA", "30"))  # access token TTL, minutes
IS_PROD = os.environ.get("ENVIRONMENT", "production") == "production"

STATE_SALT = "google-oauth-state"
EXCHANGE_CODE_TTL_SECONDS = 60  # one-time code must be redeemed quickly

state_serializer = URLSafeTimedSerializer(SECRET_KEY, salt=STATE_SALT)


# Only ever redirect within our own frontend. Prevents open-redirect via a
# crafted `redirect` query param surviving the OAuth round trip.
def _safe_redirect_path(redirect_param: str | None) -> str:
    if (
        redirect_param
        and redirect_param.startswith("/")
        and not redirect_param.startswith("//")
    ):
        return redirect_param
    return "/dashboard"


# Register Google OIDC
oauth.register(
    name="google",
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_id=os.environ["GOOGLE_CLIENT_ID"],
    client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
    client_kwargs={"scope": "openid email profile"},
)


@router.get("/api/login/google")
async def login_via_google(request: Request, redirect: str | None = None):
    try:
        safe_redirect = _safe_redirect_path(redirect)

        # Sign the intended post-login redirect into `state` so it survives
        # the round trip to Google and back, and can't be tampered with.
        state = state_serializer.dumps(
            {"redirect": safe_redirect, "nonce": secrets.token_urlsafe(16)}
        )

        redirect_uri = request.url_for("auth_via_google_callback")

        return await oauth.google.authorize_redirect(request, redirect_uri, state=state)

    except HTTPException as ht:
        await report_and_raise_500(ht, context="login_via_google: HTTP", raise_error=False)
        raise

    except Exception as e:
        await report_and_raise_500(e, context="login_via_google: Exception")
        

@router.get("/auth/google/callback")
async def auth_via_google_callback(
    request: Request, response: Response, db: AsyncSession = Depends(get_db)
):
    # Resolve the safe redirect target up front (default to /login on any
    # failure) so every error path below can still send the user somewhere
    # sane on the frontend instead of dead-ending on a JSON error blob.
    raw_state = request.query_params.get("state", "")
    safe_redirect = "/dashboard"
    try:
        state_data = state_serializer.loads(
            raw_state, max_age=600
        )  # 10 min to complete login
        safe_redirect = _safe_redirect_path(state_data.get("redirect"))
    except (BadSignature, SignatureExpired):
        return RedirectResponse(f"{FRONTEND_URL}/login?error=oauth_state_invalid")

    try:
        token = await oauth.google.authorize_access_token(request)
        user_info = token.get("userinfo")
        if not user_info:
            user_info = await oauth.google.parse_id_token(request, token)

        email = user_info.get("email")
        name = user_info.get("name")

        if not email:
            return RedirectResponse(f"{FRONTEND_URL}/login?error=oauth_no_email")

        stmt = select(User).where(User.email == email)
        result = await db.execute(stmt)
        user = result.scalar_one_or_none()

        if not user:
            # Random unusable password; this account can only ever log in via Google
            # unless/until the user sets a password through a separate flow.
            user = User(
                email=email,
                name=name,
                password=hash_password(secrets.token_urlsafe(32)),
                is_verified=True,
                tokens=1
            )
            db.add(user)
            await db.commit()

        # --- Issue tokens ---------------------------------------------------
        jwt_token = create_access_token(
            data={"sub": email}, expires_delta=timedelta(minutes=TIMEDELTA)
        )
        refresh_token = secrets.token_urlsafe(64)

        cookie_max_age = 30 * 24 * 60 * 60

        response.set_cookie(
            key="refresh_token",
            value=refresh_token,
            httponly=True,
            secure=IS_PROD,  # allow non-HTTPS only in local dev
            samesite="lax",
            max_age=cookie_max_age,
        )
        await store_refresh_token_in_db(
            db, user_email=str(user.email), token_string=refresh_token
        )

        # --- One-time exchange code -----------------------------------------
        # Never put the JWT itself in a URL (browser history, Referer headers,
        # server access logs, etc). Instead hand the frontend a short-lived,
        # single-use opaque code it must immediately exchange server-side.
        exchange_code = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=EXCHANGE_CODE_TTL_SECONDS
        )

        db.add(
            OAuthExchangeCode(
                code=exchange_code,
                access_token=jwt_token,
                expires_at=expires_at,
            )
        )
        await db.commit()

        redirect_url = (
            f"{FRONTEND_URL}/callback?code={exchange_code}&redirect={safe_redirect}"
        )
        return RedirectResponse(redirect_url)

    except OAuthError as error:
        await report_and_raise_500(
            error, context="auth_via_google_callback: OAuthError", raise_error=False
        )
        return RedirectResponse(f"{FRONTEND_URL}/login?error=oauth_failed")

    except HTTPException as ht:
        await report_and_raise_500(ht, context="auth_via_google_callback: HTTP", raise_error=False)
        raise

    except Exception as e:
        await report_and_raise_500(e, context="Auth via Google Callback: Exception")
        

@router.post("/api/auth/exchange")
async def exchange_oauth_code(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Redeem a one-time OAuth exchange code for the real access token.
    The code is deleted immediately on use (or on expiry) so it can never
    be replayed, including by anyone who might see it in browser history.
    """
    try:
        body = await request.json()
        code = body.get("code")
        if not code:
            raise HTTPException(status_code=400, detail="Missing code")

        stmt = select(OAuthExchangeCode).where(OAuthExchangeCode.code == code)
        result = await db.execute(stmt)
        record = result.scalar_one_or_none()

        if not record:
            raise HTTPException(status_code=400, detail="Invalid or already-used code")

        # Delete first so a concurrent/duplicate request can never redeem it twice,
        # then check expiry — either way the code is now consumed.
        access_token = record.access_token
        expires_at = record.expires_at
        await db.delete(record)
        await db.commit()

        if datetime.now(timezone.utc) > expires_at:
            raise HTTPException(status_code=400, detail="Code expired")

        return {"access_token": access_token, "token_type": "bearer"}

    except HTTPException as ht:
        if ht.detail in [
            "Code expired",
            "Missing code"
            "Invalid or already-used code",
        ]:
            raise
        else:
            await report_and_raise_500(
                ht, context="exchange_oauth_code: HTTP", raise_error=False
            )
            raise

    except Exception as e:
        await report_and_raise_500(e, context="exchange_oauth_code: Exception")
        