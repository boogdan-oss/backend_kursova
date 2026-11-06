import asyncio
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text

from app.collectors.registry import available_exchanges
from app.core.config import get_settings
from app.db.repositories.market import exchange_overview
from app.db.session import SessionLocal, engine
from app.pubsub import get_redis
from app.services.collector_state import read_status

router = APIRouter(tags=["health"])

CHECK_TIMEOUT_SEC = 2.0

ComponentStatus = Literal["ok", "error"]


class ComponentHealth(BaseModel):
    status: ComponentStatus
    detail: str | None = None


class ExchangeHealth(BaseModel):
    exchange: str
    status: str
    last_ok_at: datetime | None
    age_sec: float | None  # скільки секунд тому біржа востаннє відповіла
    rps: float | None  # поточна швидкість rate limiter; None — collector не працює
    limiter_state: str | None


class HealthResponse(BaseModel):
    # Лише стан API, БД і Redis: збій біржі чи зупинка collector не робить API непрацездатним
    status: Literal["ok", "degraded"]
    version: str
    time: datetime
    components: dict[str, ComponentHealth]
    exchanges: list[ExchangeHealth]


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


async def _exchanges_health() -> list[ExchangeHealth]:
    now = datetime.now(UTC)
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SEC), SessionLocal() as session:
            overviews = await exchange_overview(session, available_exchanges())
    except Exception:
        # Необов'язковий блок: /health має відповідати, навіть якщо міграції ще не застосовані
        return []
    result = []
    for overview in overviews:
        status = await read_status(get_redis(), overview.exchange)
        last_ok_at = overview.last_ok_at
        result.append(
            ExchangeHealth(
                exchange=overview.exchange,
                status=overview.status,
                last_ok_at=last_ok_at,
                age_sec=round((now - last_ok_at).total_seconds(), 1) if last_ok_at else None,
                rps=status.rps if status else None,
                limiter_state=status.limiter_state if status else None,
            )
        )
    return result


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Стан API, БД, Redis і свіжість даних бірж",
)
async def health() -> HealthResponse:
    db, redis = await asyncio.gather(_check_db(), _check_redis())
    components = {"api": ComponentHealth(status="ok"), "db": db, "redis": redis}
    overall = "ok" if all(c.status == "ok" for c in components.values()) else "degraded"
    exchanges = await _exchanges_health() if db.status == "ok" else []
    return HealthResponse(
        status=overall,
        version=get_settings().app_name,
        time=datetime.now(UTC),
        components=components,
        exchanges=exchanges,
    )


@router.get("/ping", response_model=PingResponse, summary="Перевірка з'єднання з фронтендом")
async def ping() -> PingResponse:
    return PingResponse(message="pong", time=datetime.now(UTC))
