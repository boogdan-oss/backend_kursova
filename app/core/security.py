"""Паролі й JWT.

Пароль спершу хешується SHA-256: bcrypt враховує лише перші 72 байти, а кирилиця займає
по 2 байти на літеру. Access-токен короткий і нічого не зберігає на сервері; refresh-токен
має jti, за яким сервер його відкликає (вихід, зміна пароля, повторне використання).
"""

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Literal

import bcrypt
import jwt
import structlog

from app.core.config import get_settings

ALGORITHM = "HS256"
BCRYPT_ROUNDS = 12

TokenType = Literal["access", "refresh"]

log = structlog.get_logger()


class InvalidToken(Exception):
    """Токен підроблений, прострочений або не того типу."""


@dataclass(frozen=True, slots=True)
class TokenClaims:
    user_id: int
    token_type: TokenType
    jti: str | None
    expires_at: datetime


def _prehash(password: str) -> bytes:
    return base64.b64encode(hashlib.sha256(password.encode()).digest())


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt(BCRYPT_ROUNDS)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(password), password_hash.encode())
    except ValueError:
        return False


@lru_cache
def _dev_secret() -> str:
    log.warning("auth.jwt_secret_missing", detail="JWT_SECRET не заданий: тимчасовий ключ процесу")
    return secrets.token_hex(32)


def jwt_secret() -> str:
    """Без JWT_SECRET у проді вхід не працює; локально — тимчасовий ключ до перезапуску."""
    settings = get_settings()
    if settings.jwt_secret:
        return settings.jwt_secret
    if settings.environment == "production":
        raise RuntimeError("JWT_SECRET не заданий")
    return _dev_secret()


def create_token(
    user_id: int, token_type: TokenType, ttl: timedelta, jti: str | None = None
) -> tuple[str, datetime]:
    now = datetime.now(UTC)
    expires_at = now + ttl
    claims: dict[str, object] = {
        "sub": str(user_id),
        "type": token_type,
        "iat": now,
        "exp": expires_at,
    }
    if jti is not None:
        claims["jti"] = jti
    return jwt.encode(claims, jwt_secret(), algorithm=ALGORITHM), expires_at


def create_access_token(user_id: int) -> tuple[str, datetime]:
    return create_token(user_id, "access", timedelta(minutes=get_settings().jwt_access_ttl_min))


def create_refresh_token(user_id: int) -> tuple[str, str, datetime]:
    """Повертає токен, його jti і час закінчення."""
    jti = secrets.token_hex(16)
    ttl = timedelta(days=get_settings().jwt_refresh_ttl_days)
    token, expires_at = create_token(user_id, "refresh", ttl, jti)
    return token, jti, expires_at


def decode_token(token: str, expected: TokenType) -> TokenClaims:
    try:
        claims = jwt.decode(
            token, jwt_secret(), algorithms=[ALGORITHM], options={"require": ["sub", "exp", "type"]}
        )
    except jwt.PyJWTError as exc:
        raise InvalidToken(str(exc)) from exc
    if claims.get("type") != expected:
        raise InvalidToken(f"Очікувався {expected}-токен")
    try:
        user_id = int(claims["sub"])
    except (TypeError, ValueError) as exc:
        raise InvalidToken("Некоректний sub") from exc
    jti = claims.get("jti")
    if expected == "refresh" and not isinstance(jti, str):
        raise InvalidToken("Refresh-токен без jti")
    return TokenClaims(
        user_id=user_id,
        token_type=expected,
        jti=jti if isinstance(jti, str) else None,
        expires_at=datetime.fromtimestamp(claims["exp"], UTC),
    )
