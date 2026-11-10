"""Redis: спільний клієнт і канал market:updates між collector і API.

Redis не критичний: без нього збір триває, клієнти лише не отримують live-оновлень.
"""

from collections.abc import AsyncIterator
from functools import lru_cache

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.domain import OpportunityEvent, SnapshotEvent

REDIS_TIMEOUT_SEC = 2.0
MARKET_UPDATES = "market:updates"

log = structlog.get_logger()


@lru_cache
def get_redis() -> Redis:
    return Redis.from_url(
        get_settings().redis_url,
        decode_responses=True,
        socket_connect_timeout=REDIS_TIMEOUT_SEC,
        socket_timeout=REDIS_TIMEOUT_SEC,
    )


async def close_redis() -> None:
    if get_redis.cache_info().currsize:
        await get_redis().aclose()
        get_redis.cache_clear()


async def publish_event(redis: Redis, event: SnapshotEvent | OpportunityEvent) -> None:
    try:
        await redis.publish(MARKET_UPDATES, event.model_dump_json())
    except (RedisError, OSError) as exc:
        log.warning("pubsub.publish_failed", type=event.type, error=repr(exc))


async def listen_events(redis: Redis) -> AsyncIterator[str]:
    """Повідомлення з market:updates до скасування. Читання підписки — без таймауту."""
    async with redis.pubsub() as pubsub:
        await pubsub.subscribe(MARKET_UPDATES)
        async for message in pubsub.listen():
            if message["type"] == "message":
                yield message["data"]
