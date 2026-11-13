"""Інструменти асистента: вхідні моделі й описи для Claude і Gemini.

Схема параметрів будується з тієї ж Pydantic-моделі, якою перевіряється вхід,
тож опис для моделі й перевірка не розходяться. Виконання — у services/ai_service.py.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, cast

from anthropic.types.beta import BetaToolParam
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field

Code = Annotated[str, Field(pattern=r"^[A-Za-z0-9]{1,16}$")]
Exchange = Literal["binance", "bybit", "okx", "bitget", "mexc", "htx"]
Payment = Annotated[
    str | None,
    Field(
        max_length=64, description="Банк або спосіб оплати: monobank, privatbank, abank, revolut…"
    ),
]
Amount = Annotated[
    Decimal | None,
    Field(
        gt=0,
        description="Сума угоди у фіаті: відкидає оголошення, у ліміти яких вона не вкладається",
    ),
]


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BestPricesInput(ToolInput):
    asset: Code = Field(description="Криптоактив: USDT, BTC, ETH…")
    fiat: Code = Field(description="Фіат ISO 4217: UAH, EUR, USD…")
    payment: Payment = None
    amount: Amount = None


class OffersInput(ToolInput):
    asset: Code
    fiat: Code
    side: Literal["BUY", "SELL"] = Field(
        description="BUY — оголошення, де користувач купує актив; SELL — де продає"
    )
    exchange: Exchange | None = None
    payment: Payment = None
    amount: Amount = None
    limit: int = Field(default=10, ge=1, le=20)


class OpportunitiesInput(ToolInput):
    fiat: Code
    asset: Code | None = Field(default=None, description="Без активу — усі пари фіату")
    kind: Literal["spread", "cross_exchange", "chain"] | None = Field(
        default=None,
        description=(
            "spread — купити й продати на одній біржі, cross_exchange — між біржами, "
            "chain — цикл через кілька активів; без kind — spread і cross_exchange"
        ),
    )
    min_net_spread: Decimal | None = Field(default=None, description="Мінімальний чистий спред, %")
    investment: Amount = None
    limit: int = Field(default=5, ge=1, le=10)


class CalculatorInput(ToolInput):
    asset: Code
    fiat: Code
    investment: Decimal = Field(gt=0, description="Сума інвестиції у фіаті")
    buy_exchange: Exchange = Field(description="Де купити актив")
    sell_exchange: Exchange = Field(description="Де продати актив (може бути та сама біржа)")
    payment: Payment = None


class MarketsInput(ToolInput):
    asset: Code | None = None
    fiat: Code | None = None


class HistoryInput(ToolInput):
    exchange: Exchange
    asset: Code
    fiat: Code
    hours: int = Field(default=24, ge=1, le=168, description="За скільки останніх годин")


class FiatRatesInput(ToolInput):
    currencies: list[Code] | None = Field(
        default=None,
        max_length=20,
        description="Коди ISO 4217: USD, EUR, PLN…; без них — основні валюти",
    )
    base: Code = Field(default="UAH", description="У чому виразити курс; типово гривня")
    day: date | None = Field(default=None, description="Дата YYYY-MM-DD; без неї — сьогодні")


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    model: type[ToolInput]

    def definition(self) -> BetaToolParam:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": cast(dict[str, object], self.model.model_json_schema()),
        }

    def declaration(self) -> types.FunctionDeclaration:
        return types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters_json_schema=self.model.model_json_schema(),
        )


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "get_best_prices",
        "Поточні ефективні ціни купівлі й продажу пари на кожній біржі, спред, кількість "
        "оголошень, найкраща біржа для купівлі й продажу, вік даних. Для питань «де дешевше "
        "купити», «за скільки продати», «який курс».",
        BestPricesInput,
    ),
    ToolSpec(
        "get_offers",
        "Конкретні P2P-оголошення однієї сторони, найкращі першими: ціна, ліміти, доступний "
        "обсяг, способи оплати, продавець (кількість угод, відсоток виконання).",
        OffersInput,
    ),
    ToolSpec(
        "get_opportunities",
        "Поточні арбітражні можливості після всіх комісій, найкращі першими: чистий спред, "
        "прибуток для суми, обсяг, RR, AI Score і ризик.",
        OpportunitiesInput,
    ),
    ToolSpec(
        "calculate_deal",
        "Розрахунок угоди за поточним ринком: купити на одній біржі, продати на іншій. "
        "Кількість активу, комісії, чистий прибуток, ROI, slippage, RR.",
        CalculatorInput,
    ),
    ToolSpec(
        "list_markets",
        "Які активи, фіати й способи оплати є на біржах. Для питань «чи є пара», "
        "«які банки підтримує біржа».",
        MarketsInput,
    ),
    ToolSpec(
        "get_price_history",
        "Як змінювались ефективні ціни й спред пари на біржі за останні години (до тижня).",
        HistoryInput,
    ),
    ToolSpec(
        "get_fiat_rates",
        "Офіційні курси фіатних валют НБУ на дату: скільки base коштує одиниця валюти. "
        "Для питань про курс долара, євро, злотого тощо, порівняння з P2P-курсом і "
        "перерахунку сум між валютами. Це офіційний курс, а не курс банків чи P2P.",
        FiatRatesInput,
    ),
)

TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}


def tool_definitions() -> list[BetaToolParam]:
    return [tool.definition() for tool in TOOLS]


def gemini_tools() -> list[types.Tool]:
    return [types.Tool(function_declarations=[tool.declaration() for tool in TOOLS])]
