from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import quote

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    app_name: str = "CryptoParser API"
    environment: str = "dev"
    log_level: str = "INFO"
    app_timezone: str = "Europe/Kyiv"
    db_host: str = ""
    db_port: int | None = None
    db_user: str = ""
    db_password: str = ""
    db_name: str = ""

    redis_host: str = ""
    redis_port: int | None = None
    redis_password: str = ""
    redis_db: int = 0

    database_url: str = ""
    redis_url: str = ""
    cors_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    exchanges: Annotated[list[str], NoDecode] = ["binance", "bybit", "okx", "bitget", "mexc", "htx"]
    rate_limit_initial_rps: float = 1.0
    rate_limit_max_rps: float = 10.0
    rate_limit_cooldown_sec: float = 300.0
    collector_concurrency: int = 4
    collector_priority_pairs: Annotated[list[str], NoDecode] = ["USDT/UAH"]
    collector_min_refresh_sec: float = 5.0
    data_stale_after_sec: float = 60.0
    collector_storage_gb_per_exchange: float = 2.0
    collector_min_free_disk_gb: float = 3.0

    jwt_secret: str = ""
    jwt_access_ttl_min: int = 30
    jwt_refresh_ttl_days: int = 30
    frontend_url: str = "https://kursovafrontend.vercel.app"
    public_api_url: str = "https://upars.duckdns.org"
    google_client_id: str = ""
    google_client_secret: str = ""

    telegram_bot_token: str = Field(
        default="", validation_alias=AliasChoices("TELEGRAM_BOT_TOKEN", "BOT_TOKEN")
    )
    assistant_provider: Literal["auto", "gemini", "claude"] = "auto"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash"
    gemini_fallback_model: str = "gemini-3.1-flash-lite"
    anthropic_api_key: str = ""
    assistant_model: str = "claude-opus-5-5"
    assistant_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    assistant_messages_per_hour: int = 30

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""

    @field_validator("cors_origins", "exchanges", "collector_priority_pairs", mode="before")
    @classmethod
    def split_list(cls, value: object) -> object:
        if isinstance(value, str) and not value.startswith("["):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, str):
            return [item.strip(' "') for item in value.strip("[]").split(",")]
        return value

    @model_validator(mode="after")
    def build_urls(self) -> Self:
        if not self.database_url:
            self._require(
                "DATABASE_URL",
                DB_HOST=self.db_host,
                DB_PORT=self.db_port,
                DB_USER=self.db_user,
                DB_PASSWORD=self.db_password,
                DB_NAME=self.db_name,
            )
            credentials = f"{quote(self.db_user, safe='')}:{quote(self.db_password, safe='')}"
            self.database_url = (
                f"postgresql+asyncpg://{credentials}@{self.db_host}:{self.db_port}/{self.db_name}"
            )
        if not self.redis_url:
            self._require("REDIS_URL", REDIS_HOST=self.redis_host, REDIS_PORT=self.redis_port)
            auth = f":{quote(self.redis_password, safe='')}@" if self.redis_password else ""
            self.redis_url = f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"
        return self

    @staticmethod
    def _require(url_name: str, **values: object) -> None:
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ValueError(f"Задайте в .env {url_name} або {', '.join(missing)}")
