import pytest
from pydantic import ValidationError

from app.core.settings import Settings

ENV_NAMES = (
    "DATABASE_URL",
    "REDIS_URL",
    "DB_HOST",
    "DB_PORT",
    "DB_USER",
    "DB_PASSWORD",
    "DB_NAME",
    "REDIS_HOST",
    "REDIS_PORT",
    "REDIS_PASSWORD",
)
DB_VALUES = {
    "db_host": "db",
    "db_port": 5432,
    "db_user": "user",
    "db_password": "p@ss/word",
    "db_name": "name",
}
REDIS_VALUES = {"redis_host": "cache", "redis_port": 6379}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def load(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg, arg-type]


def test_no_connection_values_live_in_code() -> None:
    with pytest.raises(ValidationError, match="DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME"):
        load()


def test_missing_redis_is_reported_by_name() -> None:
    with pytest.raises(ValidationError, match="REDIS_HOST, REDIS_PORT"):
        load(**DB_VALUES)


def test_urls_are_built_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_HOST", "from-env")
    values = {**DB_VALUES, **REDIS_VALUES}
    del values["db_host"]

    settings = load(**values, redis_password="secret")

    assert settings.database_url == "postgresql+asyncpg://user:p%40ss%2Fword@from-env:5432/name"
    assert settings.redis_url == "redis://:secret@cache:6379/0"


def test_explicit_urls_replace_separate_values() -> None:
    settings = load(database_url="postgresql+asyncpg://a:b@c/d", redis_url="redis://r:1/0")

    assert settings.database_url == "postgresql+asyncpg://a:b@c/d"
    assert settings.redis_url == "redis://r:1/0"
