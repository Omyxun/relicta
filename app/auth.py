import logging
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import User

logger = logging.getLogger(__name__)
settings = get_settings()

_password_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)

_session_serializer = URLSafeTimedSerializer(
    secret_key=settings.SECRET_KEY,
    salt="relicta-auth-session-v1",
)


class AuthenticationRequired(Exception):
    """Raised when an unauthenticated request accesses a protected route."""


def hash_password(plain_password: str) -> str:
    return _password_hasher.hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, plain_password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    try:
        return _password_hasher.check_needs_rehash(password_hash)
    except Exception:
        return False


def create_session_token(user_id: int, username: str) -> str:
    return _session_serializer.dumps({"uid": user_id, "sub": username})


def decode_session_token(token: str) -> Optional[dict[str, int | str]]:
    try:
        data = _session_serializer.loads(
            token, max_age=settings.SESSION_MAX_AGE_SECONDS
        )
        if isinstance(data, dict) and "uid" in data:
            return data
        return None
    except (SignatureExpired, BadSignature):
        return None


def set_session_cookie(
    response: Response, user: User, request: Optional[Request] = None
) -> None:
    token = create_session_token(user.id, user.username)
    is_https = False
    if request is not None:
        # Uvicorn handles forwarded headers from trusted proxies only.
        is_https = request.url.scheme == "https"

    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=token,
        max_age=settings.SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=is_https,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
    )


async def get_optional_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Optional[User]:
    token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if not token:
        return None

    payload = decode_session_token(token)
    if not payload:
        return None

    user_id = payload.get("uid")
    if not isinstance(user_id, int):
        return None

    user = await db.get(User, user_id)
    return user


async def get_current_user(
    user: Optional[User] = Depends(get_optional_user),
) -> User:
    if user is None:
        raise AuthenticationRequired()
    return user


async def get_user_count(db: AsyncSession) -> int:
    stmt = select(func.count(User.id))
    result = await db.execute(stmt)
    return int(result.scalar_one() or 0)


async def is_registration_open(db: AsyncSession) -> bool:
    """
    Registration is open if ALLOW_REGISTRATION=true OR if no users exist yet
    (so initial admin creation is never deadlocked).
    """
    if settings.ALLOW_REGISTRATION:
        return True
    count = await get_user_count(db)
    return count == 0
