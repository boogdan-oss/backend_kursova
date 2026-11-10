import asyncio
import json
from typing import Any

import pytest

from app.pubsub import MARKET_UPDATES
from app.services import market_stream
from app.services.market_stream import MarketHub, relay_events
from tests.factories import BrokenRedis, FakeRedis


def event(kind: str = "snapshot", asset: str = "USDT", fiat: str = "UAH") -> str:
    data = {"asset": asset, "fiat": fiat}
    return json.dumps({"type": kind, "ts": "2026-11-10T12:00:00Z", "data": data})


def received(queue: asyncio.Queue[str | None]) -> list[str | None]:
    messages = []
    while not queue.empty():
        messages.append(queue.get_nowait())
    return messages


@pytest.mark.parametrize(
    ("subscription", "wanted"),
    [
        (("snapshot", "USDT", "UAH"), True),
        (("snapshot", "*", "UAH"), True),
        (("snapshot", "USDT", "*"), True),
        (("snapshot", "*", "*"), True),
        (("opportunity", "USDT", "UAH"), False),
        (("snapshot", "BTC", "UAH"), False),
        (("snapshot", "USDT", "EUR"), False),
    ],
)
def test_client_matches_subscription(subscription: Any, wanted: bool) -> None:
    client = MarketHub().connect()
    client.subscriptions.add(subscription)

    assert client.wants("snapshot", "USDT", "UAH") is wanted


def test_event_reaches_only_subscribed_clients() -> None:
    hub = MarketHub()
    usdt_uah, btc_eur, silent = hub.connect(), hub.connect(), hub.connect()
    usdt_uah.subscriptions.add(("snapshot", "USDT", "UAH"))
    btc_eur.subscriptions.add(("snapshot", "BTC", "EUR"))

    assert hub.dispatch(event()) == 1
    assert received(usdt_uah.queue) == [event()]
    assert received(btc_eur.queue) == []
    assert received(silent.queue) == []


def test_event_is_forwarded_unchanged() -> None:
    hub = MarketHub()
    client = hub.connect()
    client.subscriptions.add(("opportunity", "*", "*"))
    raw = json.dumps(
        {
            "type": "opportunity",
            "ts": "2026-11-10T12:00:00Z",
            "data": {"asset": "USDT", "fiat": "UAH", "net_spread_pct": "0.8200"},
        }
    )

    hub.dispatch(raw)

    assert received(client.queue) == [raw]


@pytest.mark.parametrize(
    "raw",
    ["not json", event(kind="forecast"), json.dumps({"type": "snapshot", "data": {}})],
)
def test_bad_event_is_skipped(raw: str) -> None:
    hub = MarketHub()
    client = hub.connect()
    client.subscriptions.add(("snapshot", "*", "*"))

    assert hub.dispatch(raw) == 0
    assert received(client.queue) == []


def test_lagging_client_is_dropped_without_blocking_others() -> None:
    hub = MarketHub()
    slow, fast = hub.connect(queue_size=2), hub.connect(queue_size=10)
    for client in (slow, fast):
        client.subscriptions.add(("snapshot", "USDT", "UAH"))

    for _ in range(3):
        hub.dispatch(event())

    assert slow.lagging is True
    assert received(slow.queue) == [None]
    assert received(fast.queue) == [event()] * 3
    assert hub.clients == 1
    assert slow.push("late") is False


def test_disconnected_client_gets_nothing() -> None:
    hub = MarketHub()
    client = hub.connect()
    client.subscriptions.add(("snapshot", "USDT", "UAH"))
    hub.disconnect(client)

    assert hub.dispatch(event()) == 0
    assert hub.clients == 0


async def wait_for_message(queue: asyncio.Queue[str | None]) -> str | None:
    return await asyncio.wait_for(queue.get(), timeout=1)


async def test_relay_forwards_events_from_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = FakeRedis()
    monkeypatch.setattr(market_stream, "get_redis", lambda: redis)
    hub = MarketHub()
    client = hub.connect()
    client.subscriptions.add(("snapshot", "USDT", "UAH"))

    relay = asyncio.create_task(relay_events(hub))
    await redis.until_subscribed(MARKET_UPDATES)
    await redis.publish(MARKET_UPDATES, event())

    assert await wait_for_message(client.queue) == event()
    relay.cancel()
    await asyncio.gather(relay, return_exceptions=True)


async def test_relay_resubscribes_after_redis_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = FakeRedis()
    clients: list[Any] = [BrokenRedis(), redis]
    monkeypatch.setattr(market_stream, "get_redis", lambda: clients.pop(0) if clients else redis)
    monkeypatch.setattr(market_stream, "RECONNECT_PAUSE_SEC", 0)
    hub = MarketHub()
    client = hub.connect()
    client.subscriptions.add(("snapshot", "USDT", "UAH"))

    relay = asyncio.create_task(relay_events(hub))
    await redis.until_subscribed(MARKET_UPDATES)
    await redis.publish(MARKET_UPDATES, event())

    assert await wait_for_message(client.queue) == event()
    relay.cancel()
    await asyncio.gather(relay, return_exceptions=True)
