from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator

from app.api.deps import CODE_PATTERN, SessionDep, ensure_exchange, pair_not_found
from app.domain import Deal, Opportunity, OpportunityKind
from app.engine.arbitrage import DEFAULT_ADVERSE_MOVE_PCT
from app.engine.ranking import OpportunityFilter
from app.engine.scoring import score_category
from app.services.market_service import PairNotFound
from app.services.opportunity_service import (
    PriceUnavailable,
    calculate,
    get_opportunities,
    get_opportunity_history,
)

router = APIRouter(tags=["opportunities"])

PercentQuery = Annotated[Decimal | None, Query()]
CodeQuery = Annotated[str | None, Query(pattern=CODE_PATTERN)]


class OpportunityItem(Opportunity):
    ai_category: str | None = None


class OpportunitiesResponse(BaseModel):
    count: int
    items: list[OpportunityItem]


class OpportunityHistoryItem(OpportunityItem):
    id: int = Field(description="Ідентифікатор запису; його прикріплюють до публікації (FR-42)")
    duration_sec: int | None


class OpportunityHistoryResponse(BaseModel):
    count: int
    items: list[OpportunityHistoryItem]


class CalculatorRequest(BaseModel):
    asset: str = Field(pattern=CODE_PATTERN, examples=["USDT"])
    fiat: str = Field(pattern=CODE_PATTERN, examples=["UAH"])
    investment: Decimal = Field(gt=0, examples=[20000], description="Сума у фіаті")
    buy_exchange: str | None = Field(default=None, examples=["binance"])
    sell_exchange: str | None = Field(default=None, examples=["bybit"])
    buy_price: Decimal | None = Field(
        default=None, gt=0, description="Без неї ціна береться з глибини ринку buy_exchange"
    )
    sell_price: Decimal | None = Field(
        default=None, gt=0, description="Без неї ціна береться з глибини ринку sell_exchange"
    )
    extra_fees: Decimal = Field(default=Decimal(0), ge=0, description="Додаткові витрати у фіаті")
    adverse_move_pct: Decimal = Field(
        default=DEFAULT_ADVERSE_MOVE_PCT, ge=0, description="Рух ціни проти угоди, %, для RR"
    )
    payment: str | None = Field(default=None, description="Спосіб оплати, напр. monobank")

    @model_validator(mode="after")
    def check_price_sources(self) -> "CalculatorRequest":
        if self.buy_price is None and self.buy_exchange is None:
            raise ValueError("Вкажіть buy_price або buy_exchange")
        if self.sell_price is None and self.sell_exchange is None:
            raise ValueError("Вкажіть sell_price або sell_exchange")
        return self


class CalculatorResponse(BaseModel):
    deal: Deal
    potential_loss: Decimal
    rr: Decimal | None
    buy_slippage_pct: Decimal | None
    sell_slippage_pct: Decimal | None
    fully_filled: bool


def _item(opportunity: Opportunity) -> OpportunityItem:
    category = None if opportunity.ai_score is None else score_category(opportunity.ai_score)
    return OpportunityItem(**opportunity.model_dump(), ai_category=category)


@router.get(
    "/opportunities",
    response_model=OpportunitiesResponse,
    summary="Поточні арбітражні можливості, найкращі першими",
    description=(
        "Рахується зі свіжих (до 5 хвилин) оголошень у БД після всіх комісій. Потрібно щонайменше "
        "3 надійні оголошення на кожній стороні; спред понад 5 % вважається аномалією "
        "(нетипові оголошення), а не прибутком, і не показується.\n\n"
        "kind: spread — купити й продати на одній біржі, cross_exchange — між біржами, "
        "chain — цикл через кілька активів чи фіатів (потрібен fiat, рахується лише на "
        "окремий запит; для угоди потрібні рахунки в усіх валютах циклу). Ланцюжки кешуються "
        "для пари fiat + investment: результат до 30 с вважається свіжим, старіший віддається "
        "одразу й перераховується у фоні; detected_at показує, коли їх пораховано.\n\n"
        "Без investment сума береться типова для пари (медіана мінімальних лімітів). Комісії "
        "виводу орієнтовні, а вимоги продавців до контрагента тут не враховані, тому прибуток — "
        "верхня оцінка. Перед угодою перевіряйте все на біржі. Це аналітика, а не гарантія."
    ),
)
async def list_opportunities(
    session: SessionDep,
    asset: CodeQuery = None,
    fiat: CodeQuery = None,
    kind: OpportunityKind | None = None,
    buy_exchange: str | None = None,
    sell_exchange: str | None = None,
    min_spread: PercentQuery = None,
    min_net_spread: PercentQuery = None,
    min_ai_score: Annotated[int | None, Query(ge=0, le=100)] = None,
    min_rr: PercentQuery = None,
    min_volume: PercentQuery = None,
    investment: Annotated[Decimal | None, Query(gt=0, description="Сума у фіаті")] = None,
    hide_illiquid: bool = False,
    single_fiat: Annotated[
        bool,
        Query(description="Лише угоди без інших валют: ланцюжки через чужий фіат приховати"),
    ] = False,
) -> OpportunitiesResponse:
    for exchange in (buy_exchange, sell_exchange):
        if exchange is not None:
            ensure_exchange(exchange)
    criteria = OpportunityFilter(
        kind=kind,
        buy_exchange=buy_exchange,
        sell_exchange=sell_exchange,
        min_spread=min_spread,
        min_net_spread=min_net_spread,
        min_ai_score=min_ai_score,
        min_rr=min_rr,
        min_volume=min_volume,
        hide_illiquid=hide_illiquid,
        single_fiat=single_fiat,
    )
    try:
        found = await get_opportunities(
            session,
            asset.upper() if asset else None,
            fiat.upper() if fiat else None,
            criteria,
            investment,
        )
    except PairNotFound as exc:
        raise pair_not_found(exc) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return OpportunitiesResponse(count=len(found), items=[_item(item) for item in found])


@router.get(
    "/opportunities/history",
    response_model=OpportunityHistoryResponse,
    summary="Знайдені раніше можливості і скільки вони існували",
    description=(
        "Історію пише collector: кожна можливість — один рядок, duration_sec оновлюється, "
        "доки вона тримається (FR-19, FR-20)."
    ),
)
async def opportunities_history(
    session: SessionDep,
    asset: CodeQuery = None,
    fiat: CodeQuery = None,
    kind: OpportunityKind | None = None,
    hours: Annotated[int, Query(ge=1, le=720, description="За скільки годин")] = 24,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> OpportunityHistoryResponse:
    records = await get_opportunity_history(
        session,
        asset.upper() if asset else None,
        fiat.upper() if fiat else None,
        kind,
        hours,
        limit,
    )
    items = [
        OpportunityHistoryItem(
            **_item(record.opportunity).model_dump(),
            id=record.id,
            duration_sec=record.duration_sec,
        )
        for record in records
    ]
    return OpportunityHistoryResponse(count=len(items), items=items)


@router.post(
    "/calculator",
    response_model=CalculatorResponse,
    summary="Симулятор угоди: прибуток, комісії, ROI, RR, slippage",
    description=(
        "Задайте обидві ціни вручну або вкажіть біржі: тоді ціни й slippage рахуються "
        "з оголошень у БД на вашу суму. Реальних угод система не виконує."
    ),
)
async def deal_calculator(request: CalculatorRequest, session: SessionDep) -> CalculatorResponse:
    for exchange in (request.buy_exchange, request.sell_exchange):
        if exchange is not None:
            ensure_exchange(exchange)
    try:
        quote = await calculate(
            session,
            request.asset.upper(),
            request.fiat.upper(),
            request.investment,
            request.buy_exchange,
            request.sell_exchange,
            request.buy_price,
            request.sell_price,
            request.extra_fees,
            request.adverse_move_pct,
            request.payment,
        )
    except PairNotFound as exc:
        raise pair_not_found(exc) from exc
    except PriceUnavailable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    fills = [fill for fill in (quote.buy_fill, quote.sell_fill) if fill is not None]
    return CalculatorResponse(
        deal=quote.deal,
        potential_loss=quote.potential_loss,
        rr=quote.rr,
        buy_slippage_pct=quote.buy_fill.slippage_pct if quote.buy_fill else None,
        sell_slippage_pct=quote.sell_fill.slippage_pct if quote.sell_fill else None,
        fully_filled=all(fill.complete for fill in fills),
    )
