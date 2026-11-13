import json
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import jwt
import pytest

from app.core.settings import Settings
from app.services import google_auth_service
from app.services.google_auth_service import (
    STATE_KEY,
    GoogleAuthError,
    allowed_origin,
    parse_id_token,
    safe_next,
    start_url,
)
from tests.factories import FakeRedis

CLIENT_ID = "123.apps.googleusercontent.com"


@pytest.fixture(autouse=True)
def google_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql+asyncpg://x:y@localhost/z",
        redis_url="redis://localhost:1/0",
        google_client_id=CLIENT_ID,
        google_client_secret="secret",
        public_api_url="https://upars.duckdns.org",
        frontend_url="https://kursovafrontend.vercel.app",
        cors_origins=["https://kursovafrontend.vercel.app", "http://localhost:5173"],
    )
    monkeypatch.setattr(google_auth_service, "get_settings", lambda: settings)
    return settings


def id_token(**changes: Any) -> str:
    claims = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "1098765",
        "email": "Bohdan@Gmail.com",
        "email_verified": True,
        "name": "Богдан",
        "exp": int(time.time()) + 600,
    }
    claims.update(changes)
    return jwt.encode(claims, "google-signs-with-rs256-not-checked-here", algorithm="HS256")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (None, "/"),
        ("/alerts", "/alerts"),
        ("/offers?asset=BTC", "/offers?asset=BTC"),
        ("//evil.com", "/"),
        ("https://evil.com", "/"),
        ("/\\evil.com", "/"),
        ("alerts", "/"),
    ],
)
def test_next_path_stays_on_site(path: str | None, expected: str) -> None:
    assert safe_next(path) == expected


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        (None, "https://kursovafrontend.vercel.app"),
        ("http://localhost:5173", "http://localhost:5173"),
        ("http://localhost:5173/", "http://localhost:5173"),
        ("https://evil.com", "https://kursovafrontend.vercel.app"),
    ],
)
def test_return_only_to_known_frontend(origin: str | None, expected: str) -> None:
    assert allowed_origin(origin) == expected


async def test_start_url_asks_to_choose_account() -> None:
    redis: Any = FakeRedis()

    url = await start_url(redis, "http://localhost:5173", "/alerts")

    parsed = urlparse(url)
    query = {key: value[0] for key, value in parse_qs(parsed.query).items()}
    assert parsed.netloc == "accounts.google.com"
    assert query["prompt"] == "select_account"
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == "https://upars.duckdns.org/api/v1/auth/google/callback"
    assert query["scope"] == "openid email profile"
    assert query["code_challenge_method"] == "S256"
    saved = json.loads(redis.values[STATE_KEY.format(state=query["state"])])
    assert saved["origin"] == "http://localhost:5173" and saved["next"] == "/alerts"
    assert query["code_challenge"] != saved["verifier"]


async def test_start_without_google_keys(google_settings: Settings) -> None:
    google_settings.google_client_secret = ""

    with pytest.raises(GoogleAuthError) as failure:
        await start_url(FakeRedis(), None, None)  # type: ignore[arg-type]

    assert failure.value.code == "google_disabled"


def test_id_token_gives_profile() -> None:
    profile = parse_id_token(id_token())

    assert (profile.sub, profile.email, profile.name) == ("1098765", "bohdan@gmail.com", "Богдан")


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"aud": "someone-else"}, "google_failed"),
        ({"iss": "https://evil.com"}, "google_failed"),
        ({"exp": int(time.time()) - 10}, "google_failed"),
        ({"email_verified": False}, "google_unverified"),
        ({"email": ""}, "google_unverified"),
    ],
)
def test_bad_id_token_is_rejected(changes: dict[str, Any], code: str) -> None:
    with pytest.raises(GoogleAuthError) as failure:
        parse_id_token(id_token(**changes))

    assert failure.value.code == code


def test_garbage_id_token_is_rejected() -> None:
    with pytest.raises(GoogleAuthError):
        parse_id_token("not-a-jwt")
