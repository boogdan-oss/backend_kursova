import json
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from google.genai import errors, types

from app.ai.llm import client as llm_client
from app.ai.llm.client import AssistantDisabled, assistant_provider
from app.ai.llm.tools import TOOLS, gemini_tools
from app.services import ai_service, fiat_rates_service
from app.services.ai_service import ChatEvent, ChatTurn, chat_gemini, gemini_history, run_tool
from app.services.fiat_rates_service import NBU_URL, cross_rate, official_rates

SETTINGS = SimpleNamespace(gemini_model="gemini-main", gemini_fallback_model="gemini-lite")
NBU = [
    {"cc": "USD", "txt": "Долар США", "rate": 45.3812, "exchangedate": "13.11.2026"},
    {"cc": "EUR", "txt": "Євро", "rate": 50.3379, "exchangedate": "13.11.2026"},
    {"cc": "PLN", "txt": "Злотий", "rate": 11.8102, "exchangedate": "13.11.2026"},
]


def chunk(
    *parts: types.Part, finish: str | None = None, usage: dict[str, int] | None = None
) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=list(parts)),
                finish_reason=finish,  # type: ignore[arg-type]
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(**usage) if usage else None,
    )


def call(name: str, args: dict[str, Any], call_id: str = "c1") -> types.Part:
    return types.Part(
        function_call=types.FunctionCall(id=call_id, name=name, args=args),
        thought_signature=b"sig",
    )


class FakeGemini:
    """Підміна genai.Client: кожен виклик віддає наступну заготовку — чанки або помилку."""

    def __init__(self, *rounds: list[types.GenerateContentResponse] | Exception) -> None:
        self.rounds = list(rounds)
        self.calls: list[dict[str, Any]] = []
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content_stream=self.stream))

    async def stream(self, **kwargs: Any) -> AsyncIterator[types.GenerateContentResponse]:
        self.calls.append({**kwargs, "contents": list(kwargs["contents"])})
        current = self.rounds.pop(0)
        if isinstance(current, Exception):
            raise current

        async def chunks() -> AsyncIterator[types.GenerateContentResponse]:
            for item in current:
                yield item

        return chunks()


@pytest.fixture(autouse=True)
def settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_service, "get_settings", lambda: SETTINGS)


async def collect(client: FakeGemini, question: str = "Курс долара?") -> list[ChatEvent]:
    history = gemini_history([ChatTurn("user", question)])
    return [event async for event in chat_gemini(history, client)]  # type: ignore[arg-type]


async def test_plain_answer_streams_text_and_finishes() -> None:
    client = FakeGemini(
        [
            chunk(types.Part(text="думаю", thought=True)),
            chunk(types.Part(text="При")),
            chunk(
                types.Part(text="віт"),
                finish="STOP",
                usage={"prompt_token_count": 100, "candidates_token_count": 7},
            ),
        ]
    )

    events = await collect(client)

    assert [(e.kind, e.data.get("text")) for e in events[:2]] == [("text", "При"), ("text", "віт")]
    assert events[-1] == ChatEvent(
        "done",
        {
            "model": "gemini-main",
            "stop_reason": "stop",
            "input_tokens": 100,
            "output_tokens": 7,
            "cache_read_tokens": 0,
        },
    )
    request = client.calls[0]
    assert request["model"] == "gemini-main"
    config: types.GenerateContentConfig = request["config"]
    assert config.automatic_function_calling is not None
    assert config.automatic_function_calling.disable is True
    assert "фінанси й валюти" in str(config.system_instruction)


async def test_tool_result_goes_back_with_call_id(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_tool(name: str, args: Any) -> tuple[str, bool]:
        return json.dumps({"rate": "45.38"}), False

    monkeypatch.setattr(ai_service, "run_tool", fake_tool)
    tool_call = call("get_fiat_rates", {"currencies": ["USD"]}, "call-7")
    client = FakeGemini(
        [chunk(tool_call, finish="STOP")],
        [chunk(types.Part(text="45.38 грн"), finish="STOP")],
    )

    events = await collect(client)

    assert events[0] == ChatEvent(
        "tool", {"name": "get_fiat_rates", "input": {"currencies": ["USD"]}}
    )
    assert events[-1].kind == "done"
    contents: list[types.Content] = client.calls[1]["contents"]
    model_turn, tool_turn = contents[-2], contents[-1]
    assert model_turn.role == "model" and model_turn.parts == [tool_call]
    assert tool_turn.parts is not None
    response = tool_turn.parts[0].function_response
    assert response is not None
    assert (response.id, response.name) == ("call-7", "get_fiat_rates")
    assert response.response == {"output": {"rate": "45.38"}}


async def test_tool_error_is_passed_as_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_tool(name: str, args: Any) -> tuple[str, bool]:
        return json.dumps({"error": "Пари немає"}), True

    monkeypatch.setattr(ai_service, "run_tool", failing_tool)
    client = FakeGemini(
        [chunk(call("get_best_prices", {"asset": "ZZZ", "fiat": "UAH"}), finish="STOP")],
        [chunk(types.Part(text="Такої пари немає"), finish="STOP")],
    )

    await collect(client)

    parts = client.calls[1]["contents"][-1].parts
    assert parts[0].function_response.response == {"error": "Пари немає"}


async def test_overloaded_model_switches_to_fallback() -> None:
    busy = errors.ServerError(503, {"error": {"message": "high demand", "status": "UNAVAILABLE"}})
    client = FakeGemini(busy, [chunk(types.Part(text="Привіт"), finish="STOP")])

    events = await collect(client)

    assert [request["model"] for request in client.calls] == ["gemini-main", "gemini-lite"]
    assert events[-1].data["model"] == "gemini-lite"


async def test_other_api_errors_are_raised() -> None:
    bad = errors.ClientError(400, {"error": {"message": "bad", "status": "INVALID_ARGUMENT"}})
    client = FakeGemini(bad)

    with pytest.raises(errors.ClientError):
        await collect(client)
    assert len(client.calls) == 1


async def test_blocked_answer_is_an_error() -> None:
    client = FakeGemini([chunk(types.Part(text=""), finish="SAFETY")])

    events = await collect(client)

    assert events == [ChatEvent("error", {"detail": "Асистент не може відповісти на це питання"})]


async def test_endless_tool_calls_are_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_tool(name: str, args: Any) -> tuple[str, bool]:
        return "{}", False

    monkeypatch.setattr(ai_service, "run_tool", fake_tool)
    rounds = [
        [chunk(call("list_markets", {}), finish="STOP")] for _ in range(ai_service.MAX_TOOL_ROUNDS)
    ]
    client = FakeGemini(*rounds)

    events = await collect(client)

    assert events[-1].kind == "error"
    assert len(client.calls) == ai_service.MAX_TOOL_ROUNDS


def test_gemini_declarations_cover_all_tools() -> None:
    declarations = gemini_tools()[0].function_declarations or []

    assert [item.name for item in declarations] == [tool.name for tool in TOOLS]
    assert all(item.parameters_json_schema for item in declarations)


@pytest.mark.parametrize(
    ("choice", "gemini", "claude", "expected"),
    [
        ("auto", "g", "c", "gemini"),
        ("auto", "", "c", "claude"),
        ("claude", "g", "c", "claude"),
        ("gemini", "g", "", "gemini"),
    ],
)
def test_provider_choice(
    monkeypatch: pytest.MonkeyPatch, choice: str, gemini: str, claude: str, expected: str
) -> None:
    configured = SimpleNamespace(
        assistant_provider=choice, gemini_api_key=gemini, anthropic_api_key=claude
    )
    monkeypatch.setattr(llm_client, "get_settings", lambda: configured)

    assert assistant_provider() == expected


@pytest.mark.parametrize(
    ("choice", "missing"), [("auto", "GEMINI_API_KEY"), ("claude", "ANTHROPIC")]
)
def test_provider_without_key(monkeypatch: pytest.MonkeyPatch, choice: str, missing: str) -> None:
    configured = SimpleNamespace(assistant_provider=choice, gemini_api_key="", anthropic_api_key="")
    monkeypatch.setattr(llm_client, "get_settings", lambda: configured)

    with pytest.raises(AssistantDisabled, match=missing):
        assistant_provider()


@pytest.fixture
def nbu() -> AsyncIterator[respx.Route]:
    fiat_rates_service._cache.clear()
    with respx.mock(assert_all_called=False) as mock:
        yield mock.get(NBU_URL).mock(return_value=httpx.Response(200, json=NBU))
    fiat_rates_service._cache.clear()


async def test_official_rates_are_cached(nbu: respx.Route) -> None:
    first = await official_rates()
    second = await official_rates()

    assert first is second and nbu.call_count == 1
    assert first.day == date(2026, 11, 13)
    assert first.uah_per_unit["USD"] == Decimal("45.3812")
    assert cross_rate(first, "EUR", "USD") == Decimal("50.3379") / Decimal("45.3812")
    assert cross_rate(first, "UAH", "UAH") == 1
    assert cross_rate(first, "XYZ", "UAH") is None


async def test_rates_for_a_date_ask_nbu_for_that_date(nbu: respx.Route) -> None:
    await official_rates(date(2026, 1, 5))

    assert nbu.calls.last.request.url.params["date"] == "20260105"


async def test_fiat_rates_tool(nbu: respx.Route) -> None:
    output, failed = await run_tool("get_fiat_rates", {"currencies": ["usd", "pln", "xyz"]})
    in_euro, _ = await run_tool("get_fiat_rates", {"currencies": ["USD", "UAH"], "base": "EUR"})

    body = json.loads(output)
    assert not failed
    assert body["date"] == "2026-11-13" and body["base"] == "UAH"
    assert body["rates"] == [
        {"currency": "USD", "name": "Долар США", "rate": "45.3812"},
        {"currency": "PLN", "name": "Злотий", "rate": "11.8102"},
    ]
    assert body["missing"] == ["XYZ"]
    euro = {row["currency"]: row["rate"] for row in json.loads(in_euro)["rates"]}
    assert euro == {"USD": "0.901531", "UAH": "0.019866"}


async def test_fiat_rates_unknown_base_and_nbu_failure(nbu: respx.Route) -> None:
    unknown, failed = await run_tool("get_fiat_rates", {"base": "XYZ"})
    assert failed and "XYZ" in json.loads(unknown)["error"]

    fiat_rates_service._cache.clear()
    nbu.mock(return_value=httpx.Response(500))
    down, failed = await run_tool("get_fiat_rates", {})
    assert failed and json.loads(down)["error"] == "Сервіс курсів НБУ зараз недоступний"
