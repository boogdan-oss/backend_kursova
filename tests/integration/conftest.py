import pytest

from app.services import collect_service, market_service, opportunity_service
from tests.factories import FakeRedis


@pytest.fixture(autouse=True)
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    """Сервіси пишуть кеш і події у фейковий Redis, а не в справжній з іншого event loop."""
    fake = FakeRedis()
    for module in (collect_service, market_service, opportunity_service):
        monkeypatch.setattr(module, "get_redis", lambda: fake)
    return fake
