"""AI-асистент на Gemini або Claude: цикл «модель → інструменти → модель» і інструменти.

Інструменти читають ті самі сервіси, що й REST API (БД і кеш Redis), тож асистент бачить
ті самі дані, що й сторінки сайту. Цифри у відповідях — лише з результатів інструментів.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import (
    BetaAssistantMessageParam,
    BetaMessageParam,
    BetaToolResultBlockParam,
    BetaUserMessageParam,
)
from google import genai
from google.genai import errors as gemini_errors
from google.genai import types
from pydantic import ValidationError

from app.ai.llm.client import Provider, assistant_provider, get_claude, get_gemini
from app.ai.llm.prompts import SYSTEM_PROMPT
from app.ai.llm.tools import (
    TOOLS_BY_NAME,
    BestPricesInput,
    CalculatorInput,
    FiatRatesInput,
    HistoryInput,
    MarketsInput,
    OffersInput,
    OpportunitiesInput,
    ToolInput,
    gemini_tools,
    tool_definitions,
)
from app.collectors.payment_map import normalize_payment
from app.core.config import get_settings
from app.db.repositories.market import find_markets
from app.db.session import SessionLocal
from app.domain import Opportunity
from app.engine.ranking import OpportunityFilter
from app.engine.scoring import score_category
from app.services.fiat_rates_service import RatesUnavailable, cross_rate, official_rates
from app.services.market_service import PairNotFound, get_history, get_offers, get_snapshot
from app.services.opportunity_service import PriceUnavailable, calculate, get_opportunities

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 16000
MAX_TOOL_ROUNDS = 8
MAX_LIST_ITEMS = 60
MAIN_CURRENCIES = ("USD", "EUR", "GBP", "PLN", "CHF", "CZK", "CAD", "JPY", "CNY", "KZT")
GEMINI_SWITCH_CODES = {429, 500, 503}
BLOCKED_FINISH = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}

log = structlog.get_logger()

EventKind = Literal["text", "tool", "done", "error"]


@dataclass(frozen=True, slots=True)
class ChatEvent:
    kind: EventKind
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ChatTurn:
    """Репліка розмови від клієнта: user або assistant, лише текст."""

    role: Literal["user", "assistant"]
    text: str


class ToolFailed(Exception):
    """Інструмент не зміг відповісти; текст іде моделі як is_error."""


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _round(value: Decimal | None, digits: int = 4) -> str | None:
    return None if value is None else _plain(round(value, digits))


def _opportunity(item: Opportunity) -> dict[str, Any]:
    return {
        "kind": item.kind,
        "pair": f"{item.asset}/{item.fiat}",
        "buy_exchange": item.buy_exchange,
        "sell_exchange": item.sell_exchange,
        "buy_price": _round(item.buy_price),
        "sell_price": _round(item.sell_price),
        "spread_pct": _round(item.spread_pct, 2),
        "net_spread_pct": _round(item.net_spread_pct, 2),
        "estimated_profit": _round(item.estimated_profit, 2),
        "max_volume": _round(item.max_volume, 2),
        "rr": _round(item.rr, 2),
        "ai_score": item.ai_score,
        "ai_category": None if item.ai_score is None else score_category(item.ai_score),
        "risk_level": item.risk_level,
        "steps": [
            {
                "exchange": step.exchange,
                "action": step.action,
                "asset": step.asset,
                "price": _round(step.price),
                "fiat": step.fiat,
                "payment": step.payment_method,
                "gives": _round(step.amount_in, 8),
                "gets": _round(step.amount_out, 8),
            }
            for step in item.steps
        ],
        "detected_at": item.detected_at.isoformat(),
    }


async def best_prices(args: BestPricesInput) -> dict[str, Any]:
    asset, fiat = args.asset.upper(), args.fiat.upper()
    payment = normalize_payment(args.payment) if args.payment else None
    async with SessionLocal() as session:
        view = await get_snapshot(session, asset, fiat, None, args.amount)
    rows = [item for item in view.items if item.payment_method == payment]
    exchanges = [
        {
            "exchange": item.exchange,
            "buy_price": _round(item.buy_price),
            "sell_price": _round(item.sell_price),
            "spread_pct": _round(item.spread_pct, 2),
            "offers_buy": item.offers_count_buy,
            "offers_sell": item.offers_count_sell,
            "volume": _round(item.volume, 2),
            "fetched_at": item.fetched_at.isoformat(),
        }
        for item in rows
    ]
    buys = [item for item in rows if item.buy_price is not None]
    sells = [item for item in rows if item.sell_price is not None]
    best_buy = min(buys, key=lambda item: item.buy_price or 0, default=None)
    best_sell = max(sells, key=lambda item: item.sell_price or 0, default=None)
    return {
        "pair": f"{asset}/{fiat}",
        "payment": payment,
        "amount": _plain(args.amount),
        "exchanges": exchanges,
        "best_buy": best_buy
        and {"exchange": best_buy.exchange, "price": _round(best_buy.buy_price)},
        "best_sell": best_sell
        and {"exchange": best_sell.exchange, "price": _round(best_sell.sell_price)},
        "data_age_sec": {item.exchange: item.age_sec for item in view.freshness},
        "note": None if exchanges else "Немає оголошень з такими умовами",
    }


async def offers(args: OffersInput) -> dict[str, Any]:
    asset, fiat = args.asset.upper(), args.fiat.upper()
    async with SessionLocal() as session:
        view = await get_offers(
            session, asset, fiat, args.side, args.exchange, args.payment, args.amount
        )
    return {
        "pair": f"{asset}/{fiat}",
        "side": args.side,
        "total": len(view.offers),
        "offers": [
            {
                "exchange": offer.exchange,
                "price": _round(offer.price),
                "min_amount": _round(offer.min_amount, 2),
                "max_amount": _round(offer.max_amount, 2),
                "available": _round(offer.available, 4),
                "payments": offer.payment_methods,
                "merchant": offer.merchant.nickname,
                "orders_30d": offer.merchant.orders_30d,
                "completion_rate": _round(offer.merchant.completion_rate, 3),
            }
            for offer in view.offers[: args.limit]
        ],
        "data_age_sec": {item.exchange: item.age_sec for item in view.freshness},
    }


async def opportunities(args: OpportunitiesInput) -> dict[str, Any]:
    fiat = args.fiat.upper()
    asset = args.asset.upper() if args.asset else None
    criteria = OpportunityFilter(kind=args.kind, min_net_spread=args.min_net_spread)
    async with SessionLocal() as session:
        found = await get_opportunities(session, asset, fiat, criteria, args.investment)
    return {
        "fiat": fiat,
        "asset": asset,
        "total": len(found),
        "items": [_opportunity(item) for item in found[: args.limit]],
        "note": None if found else "Зараз немає можливостей з такими умовами після комісій",
    }


async def deal(args: CalculatorInput) -> dict[str, Any]:
    asset, fiat = args.asset.upper(), args.fiat.upper()
    async with SessionLocal() as session:
        quote = await calculate(
            session,
            asset,
            fiat,
            args.investment,
            buy_exchange=args.buy_exchange,
            sell_exchange=args.sell_exchange,
            payment=args.payment,
        )
    result = {key: _plain(value) for key, value in quote.deal.model_dump().items()}
    return {
        "pair": f"{asset}/{fiat}",
        "buy_exchange": args.buy_exchange,
        "sell_exchange": args.sell_exchange,
        "deal": result,
        "potential_loss": _plain(quote.potential_loss),
        "rr": _plain(quote.rr),
        "buy_slippage_pct": None if quote.buy_fill is None else _plain(quote.buy_fill.slippage_pct),
        "sell_slippage_pct": (
            None if quote.sell_fill is None else _plain(quote.sell_fill.slippage_pct)
        ),
        "fully_filled": all(
            fill.complete for fill in (quote.buy_fill, quote.sell_fill) if fill is not None
        ),
    }


async def markets(args: MarketsInput) -> dict[str, Any]:
    asset = args.asset.upper() if args.asset else None
    fiat = args.fiat.upper() if args.fiat else None
    async with SessionLocal() as session:
        rows = await find_markets(session, None, asset, fiat)
    if asset and fiat:
        return {
            "pair": f"{asset}/{fiat}",
            "exchanges": [
                {"exchange": row.exchange, "payments": row.payment_methods[:MAX_LIST_ITEMS]}
                for row in rows
            ],
        }
    pairs: dict[str, set[str]] = {}
    for row in rows:
        pairs.setdefault(f"{row.asset}/{row.fiat}", set()).add(row.exchange)
    return {
        "assets": sorted({row.asset for row in rows})[:MAX_LIST_ITEMS],
        "fiats": sorted({row.fiat for row in rows})[:MAX_LIST_ITEMS],
        "pairs_total": len(pairs),
        "pairs": [
            {"pair": pair, "exchanges": sorted(exchanges)}
            for pair, exchanges in sorted(pairs.items(), key=lambda item: -len(item[1]))
        ][:MAX_LIST_ITEMS],
    }


async def price_history(args: HistoryInput) -> dict[str, Any]:
    asset, fiat = args.asset.upper(), args.fiat.upper()
    end = datetime.now(UTC)
    interval = timedelta(hours=1) if args.hours > 6 else timedelta(minutes=15)
    async with SessionLocal() as session:
        points = await get_history(
            session, args.exchange, asset, fiat, end - timedelta(hours=args.hours), end, interval
        )
    return {
        "pair": f"{asset}/{fiat}",
        "exchange": args.exchange,
        "interval_min": int(interval.total_seconds() // 60),
        "points": [
            {
                "time": point.time.isoformat(),
                "buy_price": _round(point.buy_price),
                "sell_price": _round(point.sell_price),
                "spread_pct": _round(point.spread, 2),
            }
            for point in points
        ],
    }


async def fiat_rates(args: FiatRatesInput) -> dict[str, Any]:
    base = args.base.upper()
    codes = [code.upper() for code in (args.currencies or MAIN_CURRENCIES)]
    rates = await official_rates(args.day)
    if cross_rate(rates, base, base) is None:
        raise ValueError(f"Валюти {base} немає в курсах НБУ")
    found, missing = [], []
    for code in codes:
        if code == base:
            continue
        value = cross_rate(rates, code, base)
        if value is None:
            missing.append(code)
            continue
        found.append(
            {
                "currency": code,
                "name": rates.names.get(code, "Гривня" if code == "UAH" else code),
                "rate": _round(value, 4 if value >= 1 else 6),
            }
        )
    return {
        "source": "НБУ, офіційний курс (не курс банків і не P2P)",
        "date": rates.day.isoformat(),
        "base": base,
        "meaning": f"rate — скільки {base} коштує 1 одиниця валюти",
        "rates": found,
        "missing": missing,
    }


Executor = Callable[[Any], Awaitable[dict[str, Any]]]

EXECUTORS: dict[str, Executor] = {
    "get_best_prices": best_prices,
    "get_offers": offers,
    "get_opportunities": opportunities,
    "calculate_deal": deal,
    "list_markets": markets,
    "get_price_history": price_history,
    "get_fiat_rates": fiat_rates,
}


async def run_tool(name: str, raw_input: Any) -> tuple[str, bool]:
    """Виконує інструмент. Повертає JSON для моделі й чи це помилка."""
    spec = TOOLS_BY_NAME.get(name)
    if spec is None:
        return json.dumps({"error": f"Невідомий інструмент {name}"}, ensure_ascii=False), True
    try:
        args: ToolInput = spec.model.model_validate(raw_input)
        result = await EXECUTORS[name](args)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
            for error in exc.errors(include_url=False)
        )
        return json.dumps({"error": f"Невірні параметри: {problems}"}, ensure_ascii=False), True
    except PairNotFound as exc:
        detail = f"Пари {exc.asset}/{exc.fiat} немає в довіднику бірж. Доступні пари — list_markets"
        return json.dumps({"error": detail}, ensure_ascii=False), True
    except (PriceUnavailable, RatesUnavailable, ValueError) as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False), True
    except Exception as exc:
        log.error("assistant.tool_failed", tool=name, error=repr(exc))
        return json.dumps({"error": "Інструмент тимчасово недоступний"}, ensure_ascii=False), True
    return json.dumps(result, ensure_ascii=False, default=_plain), False


def echo_content(content: Sequence[Any]) -> list[Any]:
    """Блоки відповіді, які йдуть назад у наступний запит циклу.

    Після переходу на резервну модель посеред відповіді (блок fallback) thinking і
    tool_use, що були до нього, не передаються; сам блок fallback лишається на місці.
    """
    boundary = max((i for i, block in enumerate(content) if block.type == "fallback"), default=-1)
    skipped = {"thinking", "redacted_thinking", "tool_use"}
    return [block for i, block in enumerate(content) if i > boundary or block.type not in skipped]


def claude_history(turns: Sequence[ChatTurn]) -> list[BetaMessageParam]:
    return [
        BetaUserMessageParam(role="user", content=turn.text)
        if turn.role == "user"
        else BetaAssistantMessageParam(role="assistant", content=turn.text)
        for turn in turns
    ]


def gemini_history(turns: Sequence[ChatTurn]) -> list[types.Content]:
    return [
        types.Content(
            role="user" if turn.role == "user" else "model", parts=[types.Part(text=turn.text)]
        )
        for turn in turns
    ]


async def chat(
    turns: Sequence[ChatTurn], provider: Provider | None = None
) -> AsyncIterator[ChatEvent]:
    """Відповідь асистента провайдером з налаштувань (ASSISTANT_PROVIDER)."""
    chosen = provider or assistant_provider()
    stream = (
        chat_gemini(gemini_history(turns))
        if chosen == "gemini"
        else chat_claude(claude_history(turns))
    )
    async for event in stream:
        yield event


async def chat_claude(
    history: Sequence[BetaMessageParam], client: AsyncAnthropic | None = None
) -> AsyncIterator[ChatEvent]:
    """Відповідь Claude на розмову; history — повідомлення user/assistant з текстом.

    Події: text — шматок відповіді, tool — модель викликала інструмент, done — кінець,
    error — відповідь неможлива.
    """
    claude = client or get_claude()
    settings = get_settings()
    messages: list[BetaMessageParam] = list(history)
    for _ in range(MAX_TOOL_ROUNDS):
        async with claude.beta.messages.stream(
            model=settings.assistant_model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=tool_definitions(),
            messages=messages,
            output_config={"effort": settings.assistant_effort},
            cache_control={"type": "ephemeral"},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        ) as stream:
            async for event in stream:
                if event.type == "text" and event.text:
                    yield ChatEvent("text", {"text": event.text})
            response = await stream.get_final_message()

        if response.stop_reason == "refusal":
            yield ChatEvent("error", {"detail": "Асистент не може відповісти на це питання"})
            return
        content = echo_content(response.content)
        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": content})
            continue
        tool_uses = [block for block in content if block.type == "tool_use"]
        if not tool_uses:
            usage = response.usage
            yield ChatEvent(
                "done",
                {
                    "model": response.model,
                    "stop_reason": response.stop_reason,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cache_read_tokens": usage.cache_read_input_tokens or 0,
                },
            )
            return
        if response.stop_reason == "max_tokens":
            yield ChatEvent("error", {"detail": "Відповідь вийшла задовгою, уточніть питання"})
            return

        for block in tool_uses:
            yield ChatEvent("tool", {"name": block.name, "input": block.input})
        outputs = await asyncio.gather(*(run_tool(block.name, block.input) for block in tool_uses))
        messages.append({"role": "assistant", "content": content})
        results: list[BetaToolResultBlockParam] = [
            {"type": "tool_result", "tool_use_id": block.id, "content": output, "is_error": failed}
            for block, (output, failed) in zip(tool_uses, outputs, strict=True)
        ]
        messages.append(BetaUserMessageParam(role="user", content=results))
    yield ChatEvent("error", {"detail": "Забагато кроків для однієї відповіді, уточніть питання"})


def _gemini_config() -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=gemini_tools(),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        max_output_tokens=MAX_TOKENS,
    )


def _finish_name(reason: Any) -> str | None:
    if reason is None:
        return None
    return str(getattr(reason, "value", reason))


def _tool_response(call: types.FunctionCall, output: str, failed: bool) -> types.Part:
    payload = json.loads(output)
    response = payload if failed else {"output": payload}
    return types.Part(
        function_response=types.FunctionResponse(id=call.id, name=call.name, response=response)
    )


async def chat_gemini(
    history: Sequence[types.Content], client: genai.Client | None = None
) -> AsyncIterator[ChatEvent]:
    """Відповідь Gemini з тими самими інструментами й подіями, що й chat_claude.

    Якщо модель перевантажена (429 / 5xx) ще до першого слова відповіді,
    запит повторюється резервною моделлю GEMINI_FALLBACK_MODEL.
    """
    gemini = client or get_gemini()
    settings = get_settings()
    models = [settings.gemini_model]
    if settings.gemini_fallback_model and settings.gemini_fallback_model not in models:
        models.append(settings.gemini_fallback_model)
    contents: list[types.Content] = list(history)
    config = _gemini_config()
    for _ in range(MAX_TOOL_ROUNDS):
        parts: list[types.Part] = []
        finish: str | None = None
        usage: types.GenerateContentResponseUsageMetadata | None = None
        blocked = False
        while True:
            try:
                stream = await gemini.aio.models.generate_content_stream(
                    model=models[0], contents=contents, config=config
                )
                async for chunk in stream:
                    if chunk.prompt_feedback and chunk.prompt_feedback.block_reason:
                        blocked = True
                    usage = chunk.usage_metadata or usage
                    candidate = chunk.candidates[0] if chunk.candidates else None
                    if candidate is None:
                        continue
                    finish = _finish_name(candidate.finish_reason) or finish
                    for part in (candidate.content.parts if candidate.content else None) or []:
                        parts.append(part)
                        if part.text and not part.thought:
                            yield ChatEvent("text", {"text": part.text})
                break
            except gemini_errors.APIError as exc:
                if parts or exc.code not in GEMINI_SWITCH_CODES or len(models) == 1:
                    raise
                log.warning("assistant.gemini_fallback", model=models[0], status=exc.code)
                models.pop(0)

        if blocked or finish in BLOCKED_FINISH:
            yield ChatEvent("error", {"detail": "Асистент не може відповісти на це питання"})
            return
        calls = [part.function_call for part in parts if part.function_call]
        if not calls:
            if finish == "MAX_TOKENS":
                yield ChatEvent("error", {"detail": "Відповідь вийшла задовгою, уточніть питання"})
                return
            yield ChatEvent(
                "done",
                {
                    "model": models[0],
                    "stop_reason": (finish or "STOP").lower(),
                    "input_tokens": (usage.prompt_token_count or 0) if usage else 0,
                    "output_tokens": (usage.candidates_token_count or 0) if usage else 0,
                    "cache_read_tokens": (usage.cached_content_token_count or 0) if usage else 0,
                },
            )
            return

        for call in calls:
            yield ChatEvent("tool", {"name": call.name, "input": call.args or {}})
        outputs = await asyncio.gather(
            *(run_tool(call.name or "", call.args or {}) for call in calls)
        )
        contents.append(types.Content(role="model", parts=parts))
        contents.append(
            types.Content(
                role="user",
                parts=[
                    _tool_response(call, output, failed)
                    for call, (output, failed) in zip(calls, outputs, strict=True)
                ],
            )
        )
    yield ChatEvent("error", {"detail": "Забагато кроків для однієї відповіді, уточніть питання"})
