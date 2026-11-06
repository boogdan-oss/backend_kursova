import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from app.collectors.base import ExchangeAdapter, ExchangeBlocked, ExchangeError
from app.collectors.payment_map import normalize_payment
from app.collectors.ratelimit import AdaptiveRateLimiter
from app.domain import FeeSchedule, Merchant, Offer, P2PMarket, Side

BASE_URL = "https://api2.bybit.com"
ITEMS_PATH = "/fiat/otc/item/online"
CONFIG_PATH = "/fiat/p2p/config/initial"
PAYMENTS_PATH = "/fiat/otc/configuration/queryAllPaymentList"
PAGE_SIZE = 20
OK_CODE = 0
# Обмеження частоти запитів Bybit повертає кодом у тілі зі статусом 200
RATE_LIMIT_CODES = frozenset({10006, 10018})
ACTIVE_SYMBOL = 1
# side у запиті: "1" — оголошення продавців (користувач купує), "0" — покупців
SIDES: dict[Side, str] = {"BUY": "1", "SELL": "0"}
# Орієнтовний вивід дешевою мережею (TRC20 для стейблкоїнів): біржа змінює його часто
WITHDRAWAL_FEES = {
    "USDT": Decimal(1),
    "USDC": Decimal(1),
    "BTC": Decimal("0.0005"),
    "ETH": Decimal("0.005"),
}


class BybitAdapter(ExchangeAdapter):
    """Bybit P2P (неофіційні публічні ендпоінти)."""

    name = "bybit"
    base_url = BASE_URL

    def __init__(
        self,
        limiter: AdaptiveRateLimiter,
        client: httpx.AsyncClient | None = None,
        retry_base_sec: float = 0.5,
    ) -> None:
        super().__init__(limiter, client, retry_base_sec)
        self._payment_keys: dict[str, str] = {}
        self._payments_lock = asyncio.Lock()

    async def list_markets(self) -> list[P2PMarket]:
        config = self._result(await self._request("GET", CONFIG_PATH))
        fiat_payments = await self._load_payments()
        markets = []
        for symbol in config["symbols"]:
            if symbol.get("status") != ACTIVE_SYMBOL:
                continue
            fiat = str(symbol["currencyId"]).upper()
            ids = fiat_payments.get(fiat, [])
            keys = dict.fromkeys(self._payment_key(str(payment_id)) for payment_id in ids)
            markets.append(
                P2PMarket(
                    exchange=self.name,
                    asset=str(symbol["tokenId"]).upper(),
                    fiat=fiat,
                    payment_methods=list(keys),
                )
            )
        return markets

    async def fetch_offers(
        self,
        asset: str,
        fiat: str,
        side: Side,
        payment: str | None = None,
        page: int = 1,
    ) -> list[Offer]:
        if not self._payment_keys:
            async with self._payments_lock:
                if not self._payment_keys:
                    await self._load_payments()

        payload = {
            "userId": "",
            "tokenId": asset,
            "currencyId": fiat,
            "payment": [],
            "side": SIDES[side],
            "size": str(PAGE_SIZE),
            "page": str(page),
            "amount": "",
            "authMaker": False,
            "canTrade": False,
        }
        result = self._result(await self._request("POST", ITEMS_PATH, json=payload))
        fetched_at = datetime.now(UTC)
        try:
            offers = [self._parse_item(item, side, fetched_at) for item in result["items"]]
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise ExchangeError(self.name, f"формат відповіді змінився: {exc!r}") from exc
        if payment is not None:
            key = normalize_payment(payment)
            offers = [offer for offer in offers if key in offer.payment_methods]
        return offers

    @classmethod
    def fees(cls) -> FeeSchedule:
        # Тейкер на P2P комісії не платить
        return FeeSchedule(exchange=cls.name, withdrawal_fees=WITHDRAWAL_FEES)

    def _is_blocked(self, response: httpx.Response) -> bool:
        is_html = "text/html" in response.headers.get("content-type", "")
        return super()._is_blocked(response) or is_html

    def _result(self, response: httpx.Response) -> Any:
        try:
            body = response.json(parse_float=Decimal)
        except ValueError as exc:
            raise ExchangeError(self.name, f"некоректна відповідь: {exc!r}") from exc
        if not isinstance(body, dict):
            raise ExchangeError(self.name, "некоректна відповідь: очікувався об'єкт")

        code = body.get("ret_code")
        if code in RATE_LIMIT_CODES:
            self.limiter.on_blocked()
            raise ExchangeBlocked(self.name, f"біржа обмежила запити: {code}")
        if code != OK_CODE or body.get("result") is None:
            raise ExchangeError(self.name, f"помилка біржі: {code} {body.get('ret_msg')}")
        return body["result"]

    async def _load_payments(self) -> dict[str, list[int]]:
        """Оголошення містять лише id банків: назви беруться з довідника біржі."""
        result = self._result(await self._request("POST", PAYMENTS_PATH, json={}))
        self._payment_keys = {
            str(item["paymentType"]): normalize_payment(item["paymentName"])
            for item in result["paymentConfigVo"]
            if item.get("paymentName")
        }
        return json.loads(result["currencyPaymentIdMap"])

    def _payment_key(self, payment_id: str) -> str:
        # Невідомий id не відкидається: зберігається сирий ідентифікатор біржі
        return self._payment_keys.get(payment_id, f"{self.name}{payment_id}")

    def _parse_item(self, item: dict[str, Any], side: Side, fetched_at: datetime) -> Offer:
        price = Decimal(item["price"])
        available = Decimal(item["lastQuantity"])
        payments = dict.fromkeys(self._payment_key(str(pid)) for pid in item.get("payments", []))
        return Offer(
            exchange=self.name,
            side=side,
            asset=str(item["tokenId"]).upper(),
            fiat=str(item["currencyId"]).upper(),
            price=price,
            min_amount=Decimal(item["minAmount"]),
            # Заявлений ліміт буває більшим за те, що лишилось в оголошенні
            max_amount=min(Decimal(item["maxAmount"]), available * price),
            available=available,
            payment_methods=list(payments),
            # userId приходить замаскованим нулем, стабільний id — userMaskId
            merchant=Merchant(
                id=str(item["userMaskId"]),
                nickname=str(item.get("nickName", "")),
                orders_30d=int(item.get("recentOrderNum", 0)),
                completion_rate=Decimal(item.get("recentExecuteRate", 0)) / 100,
            ),
            fetched_at=fetched_at,
            external_id=str(item["id"]),
        )
