import asyncio
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import engine

router = APIRouter(tags=["health"])

CHECK_TIMEOUT_SEC = 2.0

ComponentStatus = Literal["ok", "error"]


class ComponentHealth(BaseModel):
    status: ComponentStatus
    detail: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    time: datetime
    components: dict[str, ComponentHealth]


class PingResponse(BaseModel):
    message: str
    time: datetime


async def _check_db() -> ComponentHealth:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SEC):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        return ComponentHealth(status="ok")
    except Exception as exc:
        return ComponentHealth(status="error", detail=type(exc).__name__)


async def _check_redis() -> ComponentHealth:
    client = Redis.from_url(get_settings().redis_url, socket_connect_timeout=CHECK_TIMEOUT_SEC)
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SEC):
            await client.ping()
        return ComponentHealth(status="ok")
    except Exception as exc:
        return ComponentHealth(status="error", detail=type(exc).__name__)
    finally:
        await client.aclose()


@router.get("/health", response_model=HealthResponse, summary="Стан API, БД і Redis")
async def health() -> HealthResponse:
    db, redis = await asyncio.gather(_check_db(), _check_redis())
    components = {"api": ComponentHealth(status="ok"), "db": db, "redis": redis}
    overall = "ok" if all(c.status == "ok" for c in components.values()) else "degraded"
    return HealthResponse(
        status=overall,
        version=get_settings().app_name,
        time=datetime.now(UTC),
        components=components,
    )


@router.get("/ping", response_model=PingResponse, summary="Перевірка з'єднання з фронтендом")
async def ping() -> PingResponse:
    return PingResponse(message="pong", time=datetime.now(UTC))
