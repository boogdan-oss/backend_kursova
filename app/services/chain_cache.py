"""Кеш ланцюжків у Redis.

Пошук ланцюжків бере оголошення всіх пар усіх бірж і триває секунди, тому результат
зберігається для пари (фіат, сума). Redis не критичний: без нього ланцюжки рахуються щоразу.
"""

from datetime import timedelta
from decimal import Decimal

import structlog
from pydantic import AwareDatetime, BaseModel, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.domain import Opportunity

CHAINS_KEY = "opportunities:chains:{fiat}:{amount}"
CHAINS_KEEP = timedelta(minutes=5)
ANY = "*"

log = structlog.get_logger()


class CachedChains(BaseModel):
    computed_at: AwareDatetime
    items: list[Opportunity]


def chains_key(fiat: str, investment: Decimal | None) -> str:
    """Сума нормалізується: 20000 і 20000.00 — один ключ."""
    amount = ANY if investment is None else format(investment.normalize(), "f")
    return CHAINS_KEY.format(fiat=fiat, amount=amount)


async def read_chains(redis: Redis, key: str) -> CachedChains | None:
    try:
        raw = await redis.get(key)
        return CachedChains.model_validate_json(raw) if raw else None
    except (RedisError, OSError, ValidationError) as exc:
        log.warning("opportunities.chain_cache_read_failed", error=repr(exc))
        return None


async def write_chains(redis: Redis, key: str, chains: CachedChains) -> None:
    try:
        await redis.set(key, chains.model_dump_json(), ex=int(CHAINS_KEEP.total_seconds()))
    except (RedisError, OSError) as exc:
        log.warning("opportunities.chain_cache_write_failed", error=repr(exc))
