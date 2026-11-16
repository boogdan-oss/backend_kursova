"""Redis: спільний клієнт і канал market:updates між collector і API.

Redis не критичний: без нього збір триває, клієнти лише не отримують live-оновлень.
"""

from collections.abc import AsyncIterator
from functools import lru_cache

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.domain import AlertEvent, OpportunityEvent, SnapshotEvent

REDIS_TIMEOUT_SEC = 2.0
MARKET_UPDATES = "market:updates"
POLL_SEC = 1.0
HEALTH_CHECK_SEC = 15

log = structlog.get_logger()


@lru_cache
def get_redis() -> Redis:
    return Redis.from_url(
        get_settings().redis_url,
        decode_responses=True,
        socket_connect_timeout=REDIS_TIMEOUT_SEC,
        socket_timeout=REDIS_TIMEOUT_SEC,
    )


def subscriber_redis() -> Redis:
    """Окремий клієнт для підписки: власне з'єднання, без тайм-ауту читання.

    Новий на кожну спробу, тож після збою підписка не бере з пулу те саме зламане з'єднання.
    Раз на HEALTH_CHECK_SEC з'єднання перевіряється PING, щоб мертвий сокет не висів тихо.
    """
    return Redis.from_url(
        get_settings().redis_url,
        decode_responses=True,
        socket_connect_timeout=REDIS_TIMEOUT_SEC,
        socket_keepalive=True,
        health_check_interval=HEALTH_CHECK_SEC,
    )


async def close_redis() -> None:
    if get_redis.cache_info().currsize:
        await get_redis().aclose()
        get_redis.cache_clear()


async def publish_event(redis: Redis, event: SnapshotEvent | OpportunityEvent | AlertEvent) -> None:
    try:
        await redis.publish(MARKET_UPDATES, event.model_dump_json())
    except (RedisError, OSError) as exc:
        log.warning("pubsub.publish_failed", type=event.type, error=repr(exc))


async def listen_events(redis: Redis) -> AsyncIterator[str]:
    """Повідомлення з market:updates до скасування.

    Читання короткими очікуваннями (POLL_SEC), щоб між подіями працювала перевірка з'єднання.
    """
    async with redis.pubsub() as pubsub:
        await pubsub.subscribe(MARKET_UPDATES)
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=POLL_SEC)
            if message is not None and message["type"] == "message":
                yield message["data"]
