from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from app.collectors.base import ExchangeAdapter, ExchangeError
from app.collectors.payment_map import normalize_payment
from app.domain import FeeSchedule, Merchant, Offer, P2PMarket, Side

BASE_URL = "https://www.binance.com"
AD_LIST_PATH = "/bapi/c2c/v1/public/c2c/agent/ad-list"
# Більше 20 оголошень ендпоінт не віддає; page, payType і transAmount він ігнорує
PAGE_LIMIT = 20
OK_CODE = "000000"


class BinanceAdapter(ExchangeAdapter):
    """Binance P2P (неофіційний публічний ендпоінт)."""

    name = "binance"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=10.0,
            headers={"User-Agent": "Mozilla/5.0 (CryptoParser)"},
        )

    async def list_markets(self) -> list[P2PMarket]:
        raise NotImplementedError("Довідник ринків Binance ще не реалізовано")

    async def fetch_offers(
        self,
        asset: str,
        fiat: str,
        side: Side,
        payment: str | None = None,
        page: int = 1,
    ) -> list[Offer]:
        # tradeType у Binance — з погляду користувача, як і наш side
        params: dict[str, str | int] = {
            "asset": asset,
            "fiat": fiat,
            "tradeType": side,
            "limit": PAGE_LIMIT,
        }
        try:
            response = await self._client.get(AD_LIST_PATH, params=params)
            response.raise_for_status()
            body = response.json(parse_float=Decimal)
        except (httpx.HTTPError, ValueError) as exc:
            raise ExchangeError(self.name, f"запит не вдався: {exc!r}") from exc

        if body.get("code") != OK_CODE or not isinstance(body.get("data"), dict):
            detail = f"помилка біржі: {body.get('code')} {body.get('message')}"
            raise ExchangeError(self.name, detail)

        fetched_at = datetime.now(UTC)
        items = body["data"].get("items", [])
        offers = [self._parse_item(item, side, fetched_at) for item in items]
        if payment is not None:
            key = normalize_payment(payment)
            offers = [offer for offer in offers if key in offer.payment_methods]
        return offers

    def fees(self) -> FeeSchedule:
        # Для звичайних користувачів P2P на Binance без комісії
        return FeeSchedule(exchange=self.name)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _parse_item(self, item: dict[str, Any], side: Side, fetched_at: datetime) -> Offer:
        advertiser = item.get("advertiser") or {}
        nickname = str(advertiser.get("nickName", ""))
        payments = dict.fromkeys(normalize_payment(m) for m in item.get("tradeMethods", []))
        return Offer(
            exchange=self.name,
            side=side,
            asset=str(item["asset"]).upper(),
            fiat=str(item["fiat"]).upper(),
            price=Decimal(item["price"]),
            min_amount=Decimal(item["minTransAmount"]),
            max_amount=Decimal(item["maxTransAmount"]),
            available=Decimal(item["tradableAmount"]),
            payment_methods=list(payments),
            # id продавця ендпоінт не віддає — нікнейм єдиний стабільний ідентифікатор
            merchant=Merchant(
                id=nickname,
                nickname=nickname,
                orders_30d=int(advertiser.get("monthOrderCount", 0)),
                completion_rate=Decimal(advertiser.get("monthFinishRate", 0)),
            ),
            fetched_at=fetched_at,
            external_id=item.get("adNo"),
        )
