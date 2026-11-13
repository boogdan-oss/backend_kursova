import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.domain import Freshness, MarketSnapshot
from app.services import ai_service
from app.services.ai_service import (
    FALLBACK_BETA,
    MAX_TOOL_ROUNDS,
    ChatEvent,
    chat_claude,
    echo_content,
    run_tool,
)
from app.services.market_service import PairNotFound, SnapshotView

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def text(value: str) -> Any:
    return SimpleNamespace(type="text", text=value)


def tool_use(name: str, payload: dict[str, Any], block_id: str = "t1") -> Any:
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=payload)


def turn(
    blocks: list[Any], stop_reason: str = "end_turn", streamed: list[str] | None = None
) -> Any:
    usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=80)
    final = SimpleNamespace(
        content=blocks, stop_reason=stop_reason, model="claude-opus-5-5", usage=usage
    )
    texts = streamed if streamed is not None else [b.text for b in blocks if b.type == "text"]
    return texts, final


class FakeStream:
    def __init__(self, texts: list[str], final: Any) -> None:
        self.texts = texts
        self.final = final

    async def __aenter__(self) -> "FakeStream":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._events()

    async def _events(self) -> AsyncIterator[Any]:
        for value in self.texts:
            yield SimpleNamespace(type="text", text=value)

    async def get_final_message(self) -> Any:
        return self.final


class FakeClaude:
    """Підміна AsyncAnthropic: віддає заготовлені ходи й записує запити."""

    def __init__(self, *turns: Any) -> None:
        self.turns = list(turns)
        self.calls: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self.stream))

    def stream(self, **kwargs: Any) -> FakeStream:
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        texts, final = self.turns.pop(0)
        return FakeStream(texts, final)


async def collect(client: FakeClaude, question: str = "Де купити USDT?") -> list[ChatEvent]:
    history: Any = [{"role": "user", "content": question}]
    return [event async for event in chat_claude(history, client)]  # type: ignore[arg-type]


async def test_plain_answer_streams_text_and_finishes() -> None:
    client = FakeClaude(turn([text("Привіт")], streamed=["При", "віт"]))

    events = await collect(client)

    assert [(e.kind, e.data.get("text")) for e in events[:2]] == [("text", "При"), ("text", "віт")]
    assert events[-1].kind == "done"
    assert events[-1].data["cache_read_tokens"] == 80
    request = client.calls[0]
    assert request["model"] == "claude-opus-5-5"
    assert request["fallbacks"] == "default" and request["betas"] == [FALLBACK_BETA]
    assert request["cache_control"] == {"type": "ephemeral"}
    assert request["output_config"] == {"effort": "medium"}
    assert "thinking" not in request
    assert {tool["name"] for tool in request["tools"]} >= {"get_best_prices", "get_opportunities"}


async def test_tool_call_result_goes_back_to_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    async def fake_prices(args: Any) -> dict[str, Any]:
        seen.append(args)
        return {"pair": "USDT/UAH", "best_buy": {"exchange": "bybit", "price": "41.05"}}

    monkeypatch.setitem(ai_service.EXECUTORS, "get_best_prices", fake_prices)
    call = tool_use("get_best_prices", {"asset": "USDT", "fiat": "UAH", "payment": "Mono"})
    client = FakeClaude(
        turn([text("Перевіряю ціни."), call], stop_reason="tool_use"),
        turn([text("Найдешевше на Bybit: 41.05 грн.")]),
    )

    events = await collect(client)

    assert [e.kind for e in events] == ["text", "tool", "text", "done"]
    assert events[1].data == {"name": "get_best_prices", "input": call.input}
    assert seen[0].payment == "Mono"
    follow_up = client.calls[1]["messages"]
    assert follow_up[1] == {"role": "assistant", "content": [text("Перевіряю ціни."), call]}
    [result] = follow_up[2]["content"]
    assert result["tool_use_id"] == "t1" and result["is_error"] is False
    assert json.loads(result["content"])["best_buy"]["exchange"] == "bybit"


async def test_parallel_tool_calls_return_in_one_message(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(args: Any) -> dict[str, Any]:
        return {"ok": True}

    monkeypatch.setitem(ai_service.EXECUTORS, "get_best_prices", fake)
    monkeypatch.setitem(ai_service.EXECUTORS, "get_opportunities", fake)
    calls = [
        tool_use("get_best_prices", {"asset": "USDT", "fiat": "UAH"}, "a"),
        tool_use("get_opportunities", {"fiat": "UAH"}, "b"),
    ]
    client = FakeClaude(turn(calls, stop_reason="tool_use"), turn([text("Готово")]))

    await collect(client)

    results = client.calls[1]["messages"][2]["content"]
    assert [item["tool_use_id"] for item in results] == ["a", "b"]


@pytest.mark.parametrize(
    ("name", "payload", "detail"),
    [
        ("get_weather", {}, "Невідомий інструмент"),
        ("get_best_prices", {"asset": "USDT"}, "Невірні параметри"),
        ("get_offers", {"asset": "USDT", "fiat": "UAH", "side": "HOLD"}, "side"),
        ("get_best_prices", {"asset": "USDT", "fiat": "UAH", "extra": 1}, "extra"),
    ],
)
async def test_bad_tool_calls_are_errors_for_claude(
    name: str, payload: dict[str, Any], detail: str
) -> None:
    output, failed = await run_tool(name, payload)

    assert failed is True
    assert detail in json.loads(output)["error"]


async def test_unknown_pair_is_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    async def missing(args: Any) -> dict[str, Any]:
        raise PairNotFound("DOGE", "UAH")

    monkeypatch.setitem(ai_service.EXECUTORS, "get_best_prices", missing)

    output, failed = await run_tool("get_best_prices", {"asset": "DOGE", "fiat": "UAH"})

    assert failed is True
    assert "DOGE/UAH" in json.loads(output)["error"]


async def test_unexpected_tool_failure_is_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(args: Any) -> dict[str, Any]:
        raise RuntimeError("password=secret")

    monkeypatch.setitem(ai_service.EXECUTORS, "list_markets", broken)

    output, failed = await run_tool("list_markets", {})

    assert failed is True and "secret" not in output


async def test_refusal_is_an_error() -> None:
    client = FakeClaude(turn([], stop_reason="refusal"))

    events = await collect(client)

    assert [e.kind for e in events] == ["error"]


async def test_endless_tool_calls_are_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(args: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setitem(ai_service.EXECUTORS, "list_markets", fake)
    client = FakeClaude(
        *[turn([tool_use("list_markets", {})], stop_reason="tool_use")] * MAX_TOOL_ROUNDS
    )

    events = await collect(client)

    assert events[-1].kind == "error"
    assert len(client.calls) == MAX_TOOL_ROUNDS


def test_echo_skips_blocks_before_fallback() -> None:
    thinking = SimpleNamespace(type="thinking")
    early_call = SimpleNamespace(type="tool_use")
    partial = text("Почав...")
    fallback = SimpleNamespace(type="fallback")
    late_call = SimpleNamespace(type="tool_use")

    echoed = echo_content([thinking, partial, early_call, fallback, thinking, late_call])

    assert echoed == [partial, fallback, thinking, late_call]
    assert echo_content([thinking, early_call]) == [thinking, early_call]


@asynccontextmanager
async def fake_session() -> AsyncIterator[None]:
    yield None


def snapshot(exchange: str, buy: str | None, sell: str | None, payment: str | None = None) -> Any:
    return MarketSnapshot(
        exchange=exchange,
        asset="USDT",
        fiat="UAH",
        payment_method=payment,
        buy_price=None if buy is None else Decimal(buy),
        sell_price=None if sell is None else Decimal(sell),
        fetched_at=NOW,
    )


async def test_best_prices_picks_best_exchanges(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [
        snapshot("binance", "41.30", "41.10"),
        snapshot("bybit", "41.05", "41.20"),
        snapshot("okx", None, "41.40"),
        snapshot("binance", "40.90", "41.00", "monobank"),
    ]
    freshness = [Freshness(exchange="bybit", age_sec=3.5, stale=False)]
    requests: list[Any] = []

    async def fake_snapshot(_: Any, *args: Any) -> SnapshotView:
        requests.append(args)
        return SnapshotView(items, freshness, False)

    monkeypatch.setattr(ai_service, "SessionLocal", fake_session)
    monkeypatch.setattr(ai_service, "get_snapshot", fake_snapshot)

    overall, _ = await run_tool("get_best_prices", {"asset": "usdt", "fiat": "uah"})
    mono, _ = await run_tool(
        "get_best_prices", {"asset": "USDT", "fiat": "UAH", "payment": "Mono", "amount": 5000}
    )

    body = json.loads(overall)
    assert body["best_buy"] == {"exchange": "bybit", "price": "41.05"}
    assert body["best_sell"] == {"exchange": "okx", "price": "41.4"}
    assert len(body["exchanges"]) == 3 and body["data_age_sec"] == {"bybit": 3.5}
    assert requests[0] == ("USDT", "UAH", None, None)
    mono_body = json.loads(mono)
    assert mono_body["payment"] == "monobank"
    assert [row["exchange"] for row in mono_body["exchanges"]] == ["binance"]
    assert requests[1][3] == Decimal(5000)
