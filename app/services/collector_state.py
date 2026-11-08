"""Обмін станом між API і collector через Redis.

Redis тут не критичний: без нього збір триває, а API лише не бачить стан collector
і не може підняти пріоритет пари.
"""

import time

import structlog
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.domain import CollectorStatus

STATUS_KEY = "collector:status:{exchange}"
STATUS_TTL_SEC = 30
WANTED_KEY = "collector:wanted"
WANTED_TTL_SEC = 300

log = structlog.get_logger()


async def publish_status(redis: Redis, status: CollectorStatus) -> None:
    key = STATUS_KEY.format(exchange=status.exchange)
    try:
        await redis.set(key, status.model_dump_json(), ex=STATUS_TTL_SEC)
    except (RedisError, OSError) as exc:
        log.warning("collector.status_publish_failed", error=repr(exc))


async def read_status(redis: Redis, exchange: str) -> CollectorStatus | None:
    try:
        raw = await redis.get(STATUS_KEY.format(exchange=exchange))
        return CollectorStatus.model_validate_json(raw) if raw else None
    except (RedisError, OSError, ValidationError) as exc:
        log.warning("collector.status_read_failed", error=repr(exc))
        return None


async def request_pair(redis: Redis, asset: str, fiat: str) -> bool:
    """Просить collector підняти пріоритет пари. True — запит прийнято."""
    try:
        await redis.zadd(WANTED_KEY, {f"{asset}/{fiat}": time.time()})
        return True
    except (RedisError, OSError) as exc:
        log.warning("collector.request_pair_failed", error=repr(exc))
        return False


async def wanted_pairs(redis: Redis) -> set[tuple[str, str]]:
    """Пари, які запитали через API за останні WANTED_TTL_SEC."""
    deadline = time.time() - WANTED_TTL_SEC
    try:
        await redis.zremrangebyscore(WANTED_KEY, "-inf", deadline)
        members = await redis.zrange(WANTED_KEY, 0, -1)
    except (RedisError, OSError) as exc:
        log.warning("collector.wanted_read_failed", error=repr(exc))
        return set()
    pairs = (str(member).split("/", 1) for member in members)
    return {(pair[0], pair[1]) for pair in pairs if len(pair) == 2}
