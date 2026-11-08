import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from app.collectors.base import APPROXIMATE_WITHDRAWAL_FEES, ExchangeAdapter, ExchangeError
from app.collectors.payment_map import normalize_payment
from app.collectors.ratelimit import AdaptiveRateLimiter
from app.domain import FeeSchedule, Merchant, Offer, P2PMarket, Side

BASE_URL = "https://p2p.mexc.com"
MARKET_PATH = "/api/market"
COINS_PATH = "/api/common/coins"
CURRENCIES_PATH = "/api/common/currency"
PAGE_SIZE = 20
OK_CODE = 0
ACTIVE_CURRENCY = 1
SIDES: dict[Side, str] = {"BUY": "SELL", "SELL": "BUY"}


class MexcAdapter(ExchangeAdapter):
    """MEXC P2P (неофіційні публічні ендпоінти).

    Назв способів оплати MEXC публічно не віддає, лише id: вони зберігаються як mexc<id>.
    """

    name = "mexc"
    base_url = BASE_URL

    def __init__(
        self,
        limiter: AdaptiveRateLimiter,
        client: httpx.AsyncClient | None = None,
        retry_base_sec: float = 0.5,
    ) -> None:
        super().__init__(limiter, client, retry_base_sec)
        self._coin_ids: dict[str, str] = {}
        self._coins_lock = asyncio.Lock()

    async def list_markets(self) -> list[P2PMarket]:
        await self._load_coins()
        currencies = await self._get(CURRENCIES_PATH)
        return [
            P2PMarket(
                exchange=self.name,
                asset=coin,
                fiat=str(item["currency"]).upper(),
                payment_methods=[],
            )
            for item in currencies
            if item.get("state") == ACTIVE_CURRENCY
            for coin in self._coin_ids
        ]

    async def fetch_offers(
        self,
        asset: str,
        fiat: str,
        side: Side,
        payment: str | None = None,
        page: int = 1,
    ) -> list[Offer]:
        if not self._coin_ids:
            async with self._coins_lock:
                if not self._coin_ids:
                    await self._load_coins()
        coin_id = self._coin_ids.get(asset)
        if coin_id is None:
            return []

        params = {
            "allowTrade": "false",
            "amount": "",
            "blockTrade": "false",
            "coinId": coin_id,
            "countryCode": "",
            "currency": fiat,
            "follow": "false",
            "haveTrade": "false",
            "page": page,
            "pageSize": PAGE_SIZE,
            "payMethod": "",
            "tradeType": SIDES[side],
        }
        items = await self._get(MARKET_PATH, params)
        fetched_at = datetime.now(UTC)
        try:
            offers = [self._parse_item(item, side, fiat, fetched_at) for item in items]
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise ExchangeError(self.name, f"формат відповіді змінився: {exc!r}") from exc
        if payment is not None:
            key = normalize_payment(payment)
            offers = [offer for offer in offers if key in offer.payment_methods]
        return offers

    @classmethod
    def fees(cls) -> FeeSchedule:
        return FeeSchedule(exchange=cls.name, withdrawal_fees=APPROXIMATE_WITHDRAWAL_FEES)

    async def _load_coins(self) -> None:
        coins = await self._get(COINS_PATH)
        self._coin_ids = {str(coin["coinName"]).upper(): str(coin["coinId"]) for coin in coins}

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._request("GET", path, params=params)
        try:
            body = response.json(parse_float=Decimal)
        except ValueError as exc:
            raise ExchangeError(self.name, f"некоректна відповідь: {exc!r}") from exc
        if not isinstance(body, dict):
            raise ExchangeError(self.name, "некоректна відповідь: очікувався об'єкт")
        if body.get("code") != OK_CODE or body.get("data") is None:
            raise ExchangeError(self.name, f"помилка біржі: {body.get('code')} {body.get('msg')}")
        return body["data"]

    def _parse_item(
        self, item: dict[str, Any], side: Side, fiat: str, fetched_at: datetime
    ) -> Offer:
        price = Decimal(item["price"])
        available = Decimal(item["availableQuantity"])
        merchant, stats = item["merchant"], item.get("merchantStatistics") or {}
        ids = [pid for pid in str(item.get("payMethod") or "").split(",") if pid]
        return Offer(
            exchange=self.name,
            side=side,
            asset=str(item["coinName"]).upper(),
            fiat=str(item.get("currency") or fiat).upper(),
            price=price,
            min_amount=Decimal(item["minTradeLimit"]),
            max_amount=min(Decimal(item["maxTradeLimit"]), available * price),
            available=available,
            payment_methods=list(dict.fromkeys(f"{self.name}{pid}" for pid in ids)),
            merchant=Merchant(
                id=str(merchant["memberId"]),
                nickname=str(merchant.get("nickName", "")),
                orders_30d=int(stats.get("doneLastMonthCount") or 0),
                completion_rate=Decimal(stats.get("lastMonthCompleteRate") or 0),
            ),
            fetched_at=fetched_at,
            external_id=str(item["id"]),
        )
