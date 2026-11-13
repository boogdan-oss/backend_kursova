import pytest

from app.api.v1 import ai, auth, me
from app.services import (
    alert_service,
    auth_service,
    collect_service,
    market_service,
    opportunity_service,
)
from tests.factories import FakeRedis


@pytest.fixture(autouse=True)
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    """Сервіси пишуть кеш і події у фейковий Redis, а не в справжній з іншого event loop."""
    fake = FakeRedis()
    modules = (
        collect_service,
        market_service,
        opportunity_service,
        auth_service,
        alert_service,
        me,
        auth,
        ai,
    )
    for module in modules:
        monkeypatch.setattr(module, "get_redis", lambda: fake)
    return fake
