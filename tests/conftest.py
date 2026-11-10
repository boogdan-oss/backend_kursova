from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.pubsub import get_redis


@pytest.fixture(autouse=True)
def fresh_redis_client() -> None:
    """Клієнт Redis прив'язаний до event loop, а в кожного тесту свій loop."""
    get_redis.cache_clear()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
