from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from app.collectors.base import (
    APPROXIMATE_WITHDRAWAL_FEES,
    ExchangeAdapter,
    ExchangeBlocked,
    ExchangeError,
)
from app.collectors.fiats import ISO_4217
from app.collectors.payment_map import normalize_payment
from app.domain import FeeSchedule, Merchant, Offer, P2PMarket, Side

BASE_URL = "https://www.okx.com"
BOOKS_PATH = "/v3/c2c/tradingOrders/books"
PAYMENTS_PATH = "/v3/c2c/configs/receipt/templates"
PAGE_SIZE = 20
OK_CODE = 0
UNSUPPORTED_PAIR_CODE = 17007
PROBE_ATTEMPTS = 5
SIDES: dict[Side, str] = {"BUY": "sell", "SELL": "buy"}
ASSETS = ("USDT", "USDC", "BTC", "ETH")
BOOK_FLAGS = {
    "paymentMethod": "all",
    "userType": "all",
    "showTrade": "false",
    "showFollow": "false",
    "showAlreadyTraded": "false",
    "isAbleFilter": "false",
    "receivingAds": "false",
}

log = structlog.get_logger()


class OkxAdapter(ExchangeAdapter):
    """OKX P2P (неофіційні публічні ендпоінти)."""

    name = "okx"
    base_url = BASE_URL

    async def list_markets(self) -> list[P2PMarket]:
        markets: list[P2PMarket] = []
        for fiat in ISO_4217:
            markets.extend(await self._probe_fiat(fiat))
        return markets

    async def _probe_fiat(self, fiat: str) -> list[P2PMarket]:
        """Пари одного фіату; порожньо — OKX його не підтримує.

        Перевірка ~150 фіатів довга, і OKX на ній блокує запити. Після блокування прохід чекає
        паузу rate limiter і продовжує з того ж фіату, а не обривається.
        """
        for attempt in range(PROBE_ATTEMPTS):
            try:
                body = await self._get(BOOKS_PATH, self._book_params("USDT", fiat, "BUY", 1))
                if body.get("code") == UNSUPPORTED_PAIR_CODE:
                    return []
                self._check(body)
                templates = self._check(
                    await self._get(PAYMENTS_PATH, {"quoteCurrency": fiat.lower()})
                )
            except ExchangeBlocked:
                if attempt == PROBE_ATTEMPTS - 1:
                    raise
                continue
            except ExchangeError as exc:
                log.warning("okx.market_config_failed", fiat=fiat, error=exc.detail)
                return []
            payments = dict.fromkeys(normalize_payment(item["bankCode"]) for item in templates)
            return [
                P2PMarket(
                    exchange=self.name, asset=asset, fiat=fiat, payment_methods=list(payments)
                )
                for asset in ASSETS
            ]
        return []

    async def fetch_offers(
        self,
        asset: str,
        fiat: str,
        side: Side,
        payment: str | None = None,
        page: int = 1,
    ) -> list[Offer]:
        body = await self._get(BOOKS_PATH, self._book_params(asset, fiat, side, PAGE_SIZE * page))
        if body.get("code") == UNSUPPORTED_PAIR_CODE:
            return []
        items = self._check(body).get(SIDES[side], [])[PAGE_SIZE * (page - 1) :]
        fetched_at = datetime.now(UTC)
        try:
            offers = [self._parse_item(item, side, fetched_at) for item in items]
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise ExchangeError(self.name, f"формат відповіді змінився: {exc!r}") from exc
        if payment is not None:
            key = normalize_payment(payment)
            offers = [offer for offer in offers if key in offer.payment_methods]
        return offers

    @classmethod
    def fees(cls) -> FeeSchedule:
        return FeeSchedule(exchange=cls.name, withdrawal_fees=APPROXIMATE_WITHDRAWAL_FEES)

    @staticmethod
    def _book_params(asset: str, fiat: str, side: Side, limit: int) -> dict[str, Any]:
        return {
            "quoteCurrency": fiat.lower(),
            "baseCurrency": asset.lower(),
            "side": SIDES[side],
            "limit": limit,
            **BOOK_FLAGS,
        }

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        response = await self._request("GET", path, params=params)
        try:
            body = response.json(parse_float=Decimal)
        except ValueError as exc:
            raise ExchangeError(self.name, f"некоректна відповідь: {exc!r}") from exc
        if not isinstance(body, dict):
            raise ExchangeError(self.name, "некоректна відповідь: очікувався об'єкт")
        return body

    def _check(self, body: dict[str, Any]) -> Any:
        if body.get("code") != OK_CODE or body.get("data") is None:
            raise ExchangeError(self.name, f"помилка біржі: {body.get('code')} {body.get('msg')}")
        return body["data"]

    def _parse_item(self, item: dict[str, Any], side: Side, fetched_at: datetime) -> Offer:
        price = Decimal(item["price"])
        available = Decimal(item["availableAmount"])
        payments = dict.fromkeys(normalize_payment(name) for name in item.get("paymentMethods", []))
        return Offer(
            exchange=self.name,
            side=side,
            asset=str(item["baseCurrency"]).upper(),
            fiat=str(item["quoteCurrency"]).upper(),
            price=price,
            min_amount=Decimal(item["quoteMinAmountPerOrder"]),
            max_amount=min(Decimal(item["quoteMaxAmountPerOrder"]), available * price),
            available=available,
            payment_methods=list(payments),
            merchant=Merchant(
                id=str(item["merchantId"]),
                nickname=str(item.get("nickName", "")),
                orders_30d=int(item.get("completedOrderQuantity", 0)),
                completion_rate=Decimal(item.get("completedRate") or 0),
            ),
            fetched_at=fetched_at,
            external_id=str(item["id"]),
        )
