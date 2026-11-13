"""Вхід і реєстрація через Google (OAuth 2.0 + OpenID Connect, PKCE).

1. start_url: одноразовий state і code_verifier — у Redis на 10 хвилин; браузер іде на Google
   з prompt=select_account, тож Google завжди показує вибір облікового запису.
2. finish: Google повертає code і state; code обмінюється на id_token, акаунт знаходиться
   (за google_sub або підтвердженим email) чи створюється.
3. Фронтенд отримує не токени, а одноразовий код на 60 секунд і обмінює його POST-запитом:
   токени не потрапляють в адресний рядок, історію браузера й логи.
"""

import base64
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import User
from app.db.repositories.users import (
    create_user,
    find_user_by_email,
    find_user_by_google_sub,
    get_user,
    set_google_sub,
    username_exists,
)
from app.services.auth_service import AccountBlocked, TokenPair, ensure_active, issue_tokens

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
CALLBACK_PATH = "/api/v1/auth/google/callback"
STATE_KEY = "auth:google:state:{state}"
LOGIN_KEY = "auth:google:login:{code}"
STATE_TTL_SEC = 600
LOGIN_TTL_SEC = 60
UNUSABLE_PASSWORD = "!google"
HTTP_TIMEOUT_SEC = 15

log = structlog.get_logger()


class GoogleAuthError(Exception):
    """Вхід через Google не вдався. code — для адреси фронтенду (?error=...)."""

    def __init__(self, code: str, detail: str = "", origin: str | None = None) -> None:
        super().__init__(detail or code)
        self.code = code
        self.origin = origin


@dataclass(frozen=True, slots=True)
class GoogleProfile:
    sub: str
    email: str
    name: str | None


@dataclass(frozen=True, slots=True)
class LoginRedirect:
    """Куди повернути браузер: фронтенд з одноразовим кодом входу або з помилкою."""

    origin: str
    next_path: str
    login_code: str


def google_enabled() -> bool:
    settings = get_settings()
    return bool(settings.google_client_id and settings.google_client_secret)


def redirect_uri() -> str:
    return get_settings().public_api_url.rstrip("/") + CALLBACK_PATH


def allowed_origin(origin: str | None) -> str:
    """Повертати можна лише на відомий фронтенд (CORS_ORIGINS або FRONTEND_URL)."""
    settings = get_settings()
    default = settings.frontend_url.rstrip("/")
    if origin is None:
        return default
    origin = origin.rstrip("/")
    allowed = {default, *(item.rstrip("/") for item in settings.cors_origins)}
    return origin if origin in allowed else default


def safe_next(path: str | None) -> str:
    """Лише шлях усередині сайту: «//evil.com» чи повна адреса стали б відкритим редиректом."""
    if not path or not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/"
    return path[:200]


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


async def start_url(redis: Redis, origin: str | None, next_path: str | None) -> str:
    """Адреса Google з вибором облікового запису. RedisError — вхід зараз неможливий."""
    if not google_enabled():
        raise GoogleAuthError("google_disabled", "Вхід через Google не налаштований")
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    payload = {"origin": allowed_origin(origin), "next": safe_next(next_path), "verifier": verifier}
    await redis.set(STATE_KEY.format(state=state), json.dumps(payload), ex=STATE_TTL_SEC)
    query = {
        "client_id": get_settings().google_client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256",
        "prompt": "select_account",
        "access_type": "online",
    }
    return f"{AUTHORIZE_URL}?{urlencode(query)}"


async def take_state(redis: Redis, state: str) -> dict[str, str] | None:
    raw = await redis.getdel(STATE_KEY.format(state=state))
    return None if raw is None else dict(json.loads(raw))


def parse_id_token(id_token: str) -> GoogleProfile:
    """Перевіряє вміст id_token.

    Токен отримано напряму з token endpoint Google по TLS з нашим client_secret, тож
    підпис можна не перевіряти (OpenID Connect Core, 3.1.3.7); перевіряються видавець,
    отримувач, термін дії й підтверджений email.
    """
    try:
        claims: dict[str, Any] = jwt.decode(id_token, options={"verify_signature": False})
    except jwt.PyJWTError as exc:
        raise GoogleAuthError("google_failed", "Некоректний id_token") from exc
    if claims.get("iss") not in ISSUERS or claims.get("aud") != get_settings().google_client_id:
        raise GoogleAuthError("google_failed", "id_token виданий не для цього застосунку")
    if int(claims.get("exp", 0)) < time.time():
        raise GoogleAuthError("google_failed", "id_token прострочений")
    email = str(claims.get("email") or "").strip().lower()
    if not claims.get("sub") or not email or claims.get("email_verified") is not True:
        raise GoogleAuthError("google_unverified", "Email у Google не підтверджений")
    return GoogleProfile(sub=str(claims["sub"]), email=email, name=claims.get("name"))


async def exchange_code(code: str, verifier: str, client: httpx.AsyncClient) -> GoogleProfile:
    settings = get_settings()
    try:
        response = await client.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": redirect_uri(),
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            },
            timeout=HTTP_TIMEOUT_SEC,
        )
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise GoogleAuthError(
            "google_failed", f"Google недоступний: {type(exc).__name__}"
        ) from None
    if response.status_code != 200 or "id_token" not in body:
        raise GoogleAuthError("google_failed", f"Google: {body.get('error', response.status_code)}")
    return parse_id_token(str(body["id_token"]))


_NOT_ALLOWED = re.compile(r"[^A-Za-z0-9_.]")


async def _free_username(session: AsyncSession, email: str) -> str:
    base = _NOT_ALLOWED.sub("", email.split("@", 1)[0])[:24]
    if len(base) < 3:
        base = f"user{base}"
    candidate = base
    while await username_exists(session, candidate):
        candidate = f"{base}_{secrets.randbelow(10_000):04d}"
    return candidate


async def find_or_create_user(session: AsyncSession, profile: GoogleProfile) -> User:
    """Той самий Google-акаунт — той самий користувач; інакше прив'язка за email або новий."""
    user = await find_user_by_google_sub(session, profile.sub)
    if user is None:
        user = await find_user_by_email(session, profile.email)
        if user is not None:
            await set_google_sub(session, user.id, profile.sub)
            user.google_sub = profile.sub
            log.info("auth.google_linked", user_id=user.id)
    if user is None:
        username = await _free_username(session, profile.email)
        user = await create_user(session, username, profile.email, UNUSABLE_PASSWORD)
        await set_google_sub(session, user.id, profile.sub)
        user.google_sub = profile.sub
        log.info("auth.google_registered", user_id=user.id)
    await session.commit()
    return user


async def finish(
    session: AsyncSession,
    redis: Redis,
    client: httpx.AsyncClient,
    state: str,
    code: str | None,
    error: str | None,
) -> LoginRedirect:
    """Обробляє повернення з Google. GoogleAuthError.code несе причину для фронтенду."""
    saved = await take_state(redis, state)
    if saved is None:
        raise GoogleAuthError("google_state", "Сесія входу застаріла, спробуйте ще раз")
    origin, next_path = saved["origin"], saved["next"]
    try:
        if error or not code:
            raise GoogleAuthError("google_denied" if error == "access_denied" else "google_failed")
        profile = await exchange_code(code, saved["verifier"], client)
        user = await find_or_create_user(session, profile)
        ensure_active(user)
    except AccountBlocked as exc:
        raise GoogleAuthError("account_blocked", str(exc), origin) from exc
    except GoogleAuthError as exc:
        exc.origin = origin
        raise
    except Exception as exc:
        log.error("auth.google_failed", error=repr(exc))
        raise GoogleAuthError("google_failed", origin=origin) from exc
    login_code = secrets.token_urlsafe(24)
    await redis.set(LOGIN_KEY.format(code=login_code), str(user.id), ex=LOGIN_TTL_SEC)
    return LoginRedirect(origin, next_path, login_code)


async def redeem(session: AsyncSession, redis: Redis, login_code: str) -> TokenPair:
    """Одноразовий код з адреси → пара токенів. Повторно код не спрацює."""
    raw = await redis.getdel(LOGIN_KEY.format(code=login_code))
    if raw is None:
        raise GoogleAuthError("google_state", "Код входу недійсний або застарів, увійдіть знову")
    user = await get_user(session, int(raw))
    if user is None:
        raise GoogleAuthError("google_state", "Користувача не знайдено")
    ensure_active(user)
    tokens = await issue_tokens(session, user)
    await session.commit()
    return tokens
