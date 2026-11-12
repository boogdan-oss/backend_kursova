from datetime import timedelta

import jwt
import pytest

from app.core import security
from app.core.security import (
    InvalidToken,
    create_access_token,
    create_refresh_token,
    create_token,
    decode_token,
    hash_password,
    verify_password,
)


def test_password_round_trip() -> None:
    stored = hash_password("correct horse")

    assert stored != "correct horse"
    assert verify_password("correct horse", stored)
    assert not verify_password("wrong horse", stored)


def test_long_cyrillic_password_is_not_truncated() -> None:
    base = "пароль" * 12
    stored = hash_password(base + "а")

    assert verify_password(base + "а", stored)
    assert not verify_password(base + "б", stored)


def test_broken_hash_is_not_an_error() -> None:
    assert not verify_password("anything", "not-a-bcrypt-hash")


def test_access_token_round_trip() -> None:
    token, _ = create_access_token(42)

    claims = decode_token(token, "access")

    assert claims.user_id == 42
    assert claims.jti is None


def test_refresh_token_has_jti() -> None:
    token, jti, _ = create_refresh_token(7)

    claims = decode_token(token, "refresh")

    assert (claims.user_id, claims.jti) == (7, jti)


def test_token_type_must_match() -> None:
    access, _ = create_access_token(1)
    refresh, _, _ = create_refresh_token(1)

    with pytest.raises(InvalidToken):
        decode_token(access, "refresh")
    with pytest.raises(InvalidToken):
        decode_token(refresh, "access")


def test_expired_token_is_rejected() -> None:
    token, _ = create_token(1, "access", timedelta(seconds=-1))

    with pytest.raises(InvalidToken):
        decode_token(token, "access")


def test_foreign_signature_is_rejected() -> None:
    claims = {"sub": "1", "type": "access", "exp": 4102444800}
    forged = jwt.encode(claims, "a-completely-different-secret-of-32-bytes")

    with pytest.raises(InvalidToken):
        decode_token(forged, "access")


def test_production_requires_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    class Prod:
        jwt_secret = ""
        environment = "production"
        jwt_access_ttl_min = 30

    monkeypatch.setattr(security, "get_settings", lambda: Prod())

    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        create_access_token(1)
