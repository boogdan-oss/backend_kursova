from pathlib import Path
from typing import Annotated, Self
from urllib.parse import quote

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    app_name: str = "CryptoParser API"
    environment: str = "dev"
    log_level: str = "INFO"
    app_timezone: str = "Europe/Kyiv"

    db_host: str = "localhost"
    db_port: int = 15432
    db_user: str = "p2p"
    db_password: str = "change_me"
    db_name: str = "p2p"

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = ""
    redis_db: int = 0

    # Якщо задані явно (Docker Compose), мають пріоритет над DB_* і REDIS_*
    database_url: str = ""
    redis_url: str = ""

    # Звідки фронтенд може робити запити напряму (без проксі Vite/Nginx)
    cors_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def split_origins(cls, value: object) -> object:
        if isinstance(value, str) and not value.startswith("["):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        if isinstance(value, str):
            return [origin.strip(' "') for origin in value.strip("[]").split(",")]
        return value

    @model_validator(mode="after")
    def build_urls(self) -> Self:
        if not self.database_url:
            credentials = f"{quote(self.db_user, safe='')}:{quote(self.db_password, safe='')}"
            self.database_url = (
                f"postgresql+asyncpg://{credentials}@{self.db_host}:{self.db_port}/{self.db_name}"
            )
        if not self.redis_url:
            auth = f":{quote(self.redis_password, safe='')}@" if self.redis_password else ""
            self.redis_url = f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"
        return self
