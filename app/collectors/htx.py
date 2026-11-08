import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from app.collectors.base import APPROXIMATE_WITHDRAWAL_FEES, ExchangeAdapter, ExchangeError
from app.collectors.payment_map import normalize_payment
from app.collectors.ratelimit import AdaptiveRateLimiter
from app.domain import FeeSchedule, Merchant, Offer, P2PMarket, Side

BASE_URL = "https://www.htx.com"
MARKET_PATH = "/-/x/otc/v1/data/trade-market"
CONFIG_PATH = "/-/x/otc/v1/data/config-list"
CONFIG_TYPES = "currency,marketQuery,pay"
OK_CODE = 200
HTX_PAGE_SIZE = 10
HTX_PAGES_PER_PAGE = 2
SIDES: dict[Side, str] = {"BUY": "sell", "SELL": "buy"}
MARKET_FLAGS = {
    "payMethod": 0,
    "acceptOrder": 0,
    "country": "",
    "blockType": "general",
    "online": 1,
    "range": 0,
    "amount": "",
    "onlyTradable": "false",
    "isFollowed": "false",
}


@dataclass(slots=True)
class HtxConfig:
    """Пари HTX задаються числовими id валюти й монети."""

    coins: dict[str, int] = field(default_factory=dict)
    currencies: dict[str, int] = field(default_factory=dict)
    payments: dict[str, list[str]] = field(default_factory=dict)


class HtxAdapter(ExchangeAdapter):
    """HTX (Huobi) P2P (неофіційні публічні ендпоінти)."""

    name = "htx"
    base_url = BASE_URL

    def __init__(
        self,
        limiter: AdaptiveRateLimiter,
        client: httpx.AsyncClient | None = None,
        retry_base_sec: float = 0.5,
    ) -> None:
        super().__init__(limiter, client, retry_base_sec)
        self._config = HtxConfig()
        self._config_lock = asyncio.Lock()

    async def list_markets(self) -> list[P2PMarket]:
        await self._load_config()
        return [
            P2PMarket(exchange=self.name, asset=coin, fiat=fiat, payment_methods=payments)
            for fiat, payments in self._config.payments.items()
            for coin in self._config.coins
        ]

    async def fetch_offers(
        self,
        asset: str,
        fiat: str,
        side: Side,
        payment: str | None = None,
        page: int = 1,
    ) -> list[Offer]:
        if not self._config.coins:
            async with self._config_lock:
                if not self._config.coins:
                    await self._load_config()
        coin_id, currency_id = self._config.coins.get(asset), self._config.currencies.get(fiat)
        if coin_id is None or currency_id is None:
            return []

        items: list[dict[str, Any]] = []
        first = (page - 1) * HTX_PAGES_PER_PAGE + 1
        for htx_page in range(first, first + HTX_PAGES_PER_PAGE):
            params = {
                "coinId": coin_id,
                "currency": currency_id,
                "tradeType": SIDES[side],
                "currPage": htx_page,
                **MARKET_FLAGS,
            }
            batch = await self._get(MARKET_PATH, params)
            items.extend(batch)
            if len(batch) < HTX_PAGE_SIZE:
                break

        fetched_at = datetime.now(UTC)
        try:
            offers = [self._parse_item(item, side, asset, fiat, fetched_at) for item in items]
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise ExchangeError(self.name, f"формат відповіді змінився: {exc!r}") from exc
        if payment is not None:
            key = normalize_payment(payment)
            offers = [offer for offer in offers if key in offer.payment_methods]
        return offers

    @classmethod
    def fees(cls) -> FeeSchedule:
        return FeeSchedule(exchange=cls.name, withdrawal_fees=APPROXIMATE_WITHDRAWAL_FEES)

    async def _load_config(self) -> None:
        data = await self._get(CONFIG_PATH, {"type": CONFIG_TYPES})
        names = {item["payMethodId"]: item["name"] for item in data.get("payMethod") or []}
        p2p_currencies = [item for item in data.get("currency") or [] if item.get("showPtoP")]
        self._config = HtxConfig(
            coins={
                str(item["coinName"]).upper(): int(item["coinId"]) for item in data["marketQuery"]
            },
            currencies={
                str(item["nameShort"]).upper(): int(item["currencyId"]) for item in p2p_currencies
            },
            payments={
                str(item["nameShort"]).upper(): list(
                    dict.fromkeys(
                        normalize_payment(names[pid])
                        for pid in item.get("supportPayments") or []
                        if pid in names
                    )
                )
                for item in p2p_currencies
            },
        )

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        response = await self._request("GET", path, params=params)
        try:
            body = response.json(parse_float=Decimal)
        except ValueError as exc:
            raise ExchangeError(self.name, f"некоректна відповідь: {exc!r}") from exc
        if not isinstance(body, dict):
            raise ExchangeError(self.name, "некоректна відповідь: очікувався об'єкт")
        if body.get("code") != OK_CODE or body.get("data") is None:
            raise ExchangeError(
                self.name, f"помилка біржі: {body.get('code')} {body.get('message')}"
            )
        return body["data"]

    def _parse_item(
        self, item: dict[str, Any], side: Side, asset: str, fiat: str, fetched_at: datetime
    ) -> Offer:
        price = Decimal(item["price"])
        available = Decimal(item["tradeCount"])
        methods = item.get("payMethods") or []
        payments = dict.fromkeys(normalize_payment(method["name"]) for method in methods)
        return Offer(
            exchange=self.name,
            side=side,
            asset=asset,
            fiat=fiat,
            price=price,
            min_amount=Decimal(item["minTradeLimit"]),
            max_amount=min(Decimal(item["maxTradeLimit"]), available * price),
            available=available,
            payment_methods=list(payments),
            merchant=Merchant(
                id=str(item["uid"]),
                nickname=str(item.get("userName", "")),
                orders_30d=int(item.get("tradeMonthTimes") or 0),
                completion_rate=Decimal(item.get("orderCompleteRate") or 0) / 100,
            ),
            fetched_at=fetched_at,
            external_id=str(item["id"]),
        )
