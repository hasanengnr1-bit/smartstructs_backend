import os
import resend
import random
from jose import jwt  # type: ignore[import-untyped]
from sqlalchemy import select
from passlib.context import CryptContext  # type: ignore[import-untyped]
from datetime import timedelta, datetime
from fastapi.security import OAuth2PasswordBearer
from fastapi import HTTPException, Depends, status, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from database import User, get_db, RefreshToken
from sqlalchemy.ext.asyncio import AsyncSession

# Configuration constants
SECRET_KEY = os.environ['ENCRYPTION_SECRET_KEY']
ALGORITHM = "HS256"
RESET_TOKEN_SALT = os.environ.get('RESET_TOKEN_SALT')
RESET_TOKEN_MAX_AGE = 15 * 60  # 15 minutes

reset_serializer = URLSafeTimedSerializer(os.environ["APP_SECRET"])
# Password hashing configuration
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

# OAuth2 scheme for token extraction
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

def generate_otp() -> str:
    return f"{random.randint(100000, 999995):06d}"

resend.api_key = os.getenv("RESEND_API_KEY")
async def send_otp_email(to_email: str, otp: str):
    params: resend.Emails.SendParams = {
        "from": "onboarding@smartstructs.org",
        "to": [to_email],
        "subject": "SmartStructs Email Verification",
        "html": f"""
            <div style="font-family: sans-serif; padding: 20px;">
                <h2>Welcome to SmartStructs!</h2>
                <p>Please use the following One-Time Password (OTP) to verify your account:</p>
                <h1 style="background: #f4f4f4; padding: 10px; display: inline-block; letter-spacing: 2px;">{otp}</h1>
                <p>This code will expire in 10 minutes.</p>
            </div>
        """
    }
    # Notice the '_async' suffix - this keeps your FastAPI loop non-blocking
    await resend.Emails.send_async(params)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def create_access_token(data: dict, expires_delta: timedelta) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + expires_delta
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


async def get_current_user(
    db: AsyncSession = Depends(get_db),    
    token: str = Depends(oauth2_scheme)
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
            headers={"WWW-Authenticate": "Bearer error=\"invalid_token\", error_description=\"Token expired\""}
        )
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

        
    db_result = await db.execute(select(User).filter(User.email == email))
    user = db_result.scalar_one_or_none()
    
    if user is None:
        raise credentials_exception
        
    return user

async def store_refresh_token_in_db(db: AsyncSession, user_email: str, token_string: str) -> None:
    """Saves the token to the database with a 30-day expiration."""
    expires_at = datetime.utcnow() + timedelta(days=30) 
    
    db_token = RefreshToken(
        token=token_string,
        user_email=user_email,
        expires_at=expires_at,
        revoked=False
    )
    db.add(db_token)
    await db.commit()


async def get_reset_token_email(request: Request) -> str:
    token = request.cookies.get("reset_token")
    if not token:
        raise HTTPException(status_code=401, detail="Reset session missing or expired. Please start over.")
    try:
        return reset_serializer.loads(token, salt=RESET_TOKEN_SALT, max_age=RESET_TOKEN_MAX_AGE)
    except SignatureExpired:
        raise HTTPException(status_code=401, detail="Reset session has expired. Please start over.")
    except BadSignature:
        raise HTTPException(status_code=401, detail="Invalid reset session. Please start over.")
