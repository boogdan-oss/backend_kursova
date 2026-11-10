import json
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from app.api import ws as ws_module
from app.api.ws import LAGGING_CLOSE_CODE, MAX_SUBSCRIPTIONS
from app.domain import MarketSnapshot, SnapshotEvent, SnapshotUpdate
from app.main import app
from app.pubsub import MARKET_UPDATES
from app.services import market_stream
from app.services.market_stream import MarketClient, MarketHub
from tests.factories import FakeRedis

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
URL = "/ws/market"


def snapshot_event(asset: str = "USDT", fiat: str = "UAH", price: str = "41.25") -> str:
    item = MarketSnapshot(
        exchange="binance", asset=asset, fiat=fiat, buy_price=Decimal(price), fetched_at=NOW
    )
    update = SnapshotUpdate(exchange="binance", asset=asset, fiat=fiat, items=[item])
    return SnapshotEvent(ts=NOW, data=update).model_dump_json()


def opportunity_event(asset: str = "USDT", fiat: str = "UAH") -> str:
    data = {"kind": "cross_exchange", "asset": asset, "fiat": fiat, "net_spread_pct": "0.82"}
    return json.dumps({"type": "opportunity", "ts": "2026-11-10T12:00:00Z", "data": data})


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(market_stream, "get_redis", lambda: fake)
    return fake


@pytest.fixture
def api(redis: FakeRedis) -> Iterator[TestClient]:
    with TestClient(app) as client:
        call(client, redis.until_subscribed, MARKET_UPDATES)
        yield client


def call(client: TestClient, function: Any, *args: Any) -> Any:
    assert client.portal is not None
    return client.portal.call(function, *args)


def publish(api: TestClient, redis: FakeRedis, raw: str) -> None:
    call(api, redis.publish, MARKET_UPDATES, raw)


def subscribe(socket: WebSocketTestSession, *pairs: tuple[str, str], **extra: Any) -> Any:
    pair_refs = [{"asset": asset, "fiat": fiat} for asset, fiat in pairs]
    socket.send_json({"action": "subscribe", "pairs": pair_refs, **extra})
    return socket.receive_json()


def test_subscribe_confirms_all_subscriptions(api: TestClient) -> None:
    with api.websocket_connect(URL) as socket:
        reply = subscribe(socket, ("usdt", "uah"))

    assert reply["type"] == "subscribed"
    assert reply["ts"].endswith("Z")
    assert reply["data"]["subscriptions"] == [
        {"type": "opportunity", "asset": "USDT", "fiat": "UAH"},
        {"type": "snapshot", "asset": "USDT", "fiat": "UAH"},
    ]


def test_client_gets_only_events_of_its_pairs(api: TestClient, redis: FakeRedis) -> None:
    with api.websocket_connect(URL) as socket:
        subscribe(socket, ("USDT", "UAH"))
        publish(api, redis, snapshot_event("BTC", "UAH"))
        publish(api, redis, snapshot_event("USDT", "EUR"))
        publish(api, redis, snapshot_event("USDT", "UAH"))

        message = socket.receive_text()

    assert message == snapshot_event("USDT", "UAH")
    body = json.loads(message)
    assert body["type"] == "snapshot"
    assert body["data"]["items"][0]["buy_price"] == "41.25"


def test_types_and_wildcard_narrow_subscription(api: TestClient, redis: FakeRedis) -> None:
    with api.websocket_connect(URL) as socket:
        reply = subscribe(socket, ("*", "UAH"), types=["opportunity"])
        publish(api, redis, snapshot_event("BTC", "UAH"))
        publish(api, redis, opportunity_event("BTC", "EUR"))
        publish(api, redis, opportunity_event("BTC", "UAH"))

        message = socket.receive_json()

    assert reply["data"]["subscriptions"] == [{"type": "opportunity", "asset": "*", "fiat": "UAH"}]
    assert message["type"] == "opportunity"
    assert message["data"]["asset"] == "BTC"


def test_unsubscribe_stops_events(api: TestClient, redis: FakeRedis) -> None:
    with api.websocket_connect(URL) as socket:
        subscribe(socket, ("USDT", "UAH"), ("BTC", "UAH"))
        socket.send_json({"action": "unsubscribe", "pairs": [{"asset": "USDT", "fiat": "UAH"}]})
        reply = socket.receive_json()
        publish(api, redis, snapshot_event("USDT", "UAH"))
        publish(api, redis, snapshot_event("BTC", "UAH"))

        message = socket.receive_json()

    assert {item["asset"] for item in reply["data"]["subscriptions"]} == {"BTC"}
    assert message["data"]["asset"] == "BTC"


def test_each_client_has_own_subscriptions(api: TestClient, redis: FakeRedis) -> None:
    with api.websocket_connect(URL) as uah, api.websocket_connect(URL) as eur:
        subscribe(uah, ("USDT", "UAH"))
        subscribe(eur, ("USDT", "EUR"))
        publish(api, redis, snapshot_event("USDT", "UAH"))
        publish(api, redis, snapshot_event("USDT", "EUR"))

        assert uah.receive_json()["data"]["fiat"] == "UAH"
        assert eur.receive_json()["data"]["fiat"] == "EUR"


@pytest.mark.parametrize(
    ("command", "problem"),
    [
        ("not json", "json"),
        ({"action": "buy", "pairs": [{"asset": "USDT", "fiat": "UAH"}]}, "action"),
        ({"action": "subscribe", "pairs": []}, "pairs"),
        ({"action": "subscribe", "pairs": [{"asset": "USDT; DROP", "fiat": "UAH"}]}, "asset"),
        (
            {"action": "subscribe", "pairs": [{"asset": "USDT", "fiat": "UAH"}], "types": ["x"]},
            "types",
        ),
    ],
)
def test_bad_command_gets_error_and_connection_stays(
    api: TestClient, command: Any, problem: str
) -> None:
    with api.websocket_connect(URL) as socket:
        if isinstance(command, str):
            socket.send_text(command)
        else:
            socket.send_json(command)
        error = socket.receive_json()
        reply = subscribe(socket, ("USDT", "UAH"))

    assert error["type"] == "error"
    assert error["data"]["detail"].startswith("Невірна команда")
    assert problem in error["data"]["detail"]
    assert reply["type"] == "subscribed"


def test_binary_message_gets_error(api: TestClient) -> None:
    with api.websocket_connect(URL) as socket:
        socket.send_bytes(b"{}")

        error = socket.receive_json()

    assert error["type"] == "error"


def test_subscriptions_are_limited(api: TestClient) -> None:
    pairs = [(f"COIN{index}", "UAH") for index in range(MAX_SUBSCRIPTIONS // 2)]
    with api.websocket_connect(URL) as socket:
        accepted = subscribe(socket, *pairs)
        rejected = subscribe(socket, ("USDT", "UAH"))

    assert len(accepted["data"]["subscriptions"]) == MAX_SUBSCRIPTIONS
    assert rejected["type"] == "error"
    assert str(MAX_SUBSCRIPTIONS) in rejected["data"]["detail"]


def test_lagging_client_is_disconnected(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    class SmallHub(MarketHub):
        def connect(self, queue_size: int = 2) -> MarketClient:
            return super().connect(queue_size)

    hub = SmallHub()
    monkeypatch.setattr(ws_module, "hub", hub)

    def flood() -> None:
        for _ in range(5):
            hub.dispatch(snapshot_event())

    with api.websocket_connect(URL) as socket:
        subscribe(socket, ("USDT", "UAH"))
        call(api, flood)

        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_text()

    assert closed.value.code == LAGGING_CLOSE_CODE
    assert hub.clients == 0


def test_closed_connection_leaves_hub(api: TestClient) -> None:
    with api.websocket_connect(URL) as socket:
        subscribe(socket, ("USDT", "UAH"))
        assert ws_module.hub.clients == 1

    for _ in range(100):
        if ws_module.hub.clients == 0:
            break
        call(api, _tick)
    assert ws_module.hub.clients == 0


async def _tick() -> None:
    return None
