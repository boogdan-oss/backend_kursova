"""Кеш знімків ринку в Redis.

Знімок прив'язаний до версії даних — часу останнього збору кожної біржі. Нові оголошення
змінюють версію, тож кеш не віддає застарілих цін. Redis не критичний: без нього API
рахує знімок з БД.
"""

from decimal import Decimal

import structlog
from pydantic import BaseModel, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.domain import MarketSnapshot

SNAPSHOT_KEY = "market:snapshot:{asset}/{fiat}:{exchange}:{amount}"
SNAPSHOT_TTL_SEC = 60
ANY = "*"

log = structlog.get_logger()


class CachedSnapshot(BaseModel):
    version: str
    items: list[MarketSnapshot]


def snapshot_key(asset: str, fiat: str, exchange: str | None, amount: Decimal | None) -> str:
    """Сума нормалізується: 5000 і 5000.00 — один ключ."""
    return SNAPSHOT_KEY.format(
        asset=asset,
        fiat=fiat,
        exchange=exchange or ANY,
        amount=ANY if amount is None else format(amount.normalize(), "f"),
    )


async def read_snapshot(redis: Redis, key: str, version: str) -> list[MarketSnapshot] | None:
    """None — знімка немає, він старішої версії або Redis недоступний."""
    try:
        raw = await redis.get(key)
        cached = CachedSnapshot.model_validate_json(raw) if raw else None
    except (RedisError, OSError, ValidationError) as exc:
        log.warning("market.snapshot_cache_read_failed", error=repr(exc))
        return None
    if cached is None or cached.version != version:
        return None
    return cached.items


async def write_snapshot(redis: Redis, key: str, version: str, items: list[MarketSnapshot]) -> None:
    value = CachedSnapshot(version=version, items=items).model_dump_json()
    try:
        await redis.set(key, value, ex=SNAPSHOT_TTL_SEC)
    except (RedisError, OSError) as exc:
        log.warning("market.snapshot_cache_write_failed", error=repr(exc))
