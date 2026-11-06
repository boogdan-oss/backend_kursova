from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import structlog

from app.collectors.base import ExchangeAdapter, ExchangeBlocked, ExchangeError
from app.collectors.payment_map import normalize_payment
from app.domain import FeeSchedule, Merchant, Offer, P2PMarket, Side

BASE_URL = "https://www.binance.com"
SEARCH_PATH = "/bapi/c2c/v2/friendly/c2c/adv/search"
FIAT_LIST_PATH = "/bapi/c2c/v1/friendly/c2c/trade-rule/fiat-list"
CONFIG_PATH = "/bapi/c2c/v2/friendly/c2c/portal/config"
PAGE_SIZE = 20
OK_CODE = "000000"
P2P_AREA = "P2P"
# fiat-list їх не віддає, хоча оголошення є; portal/config для непідтримуваних повертає порожньо
EXTRA_FIATS = ("CZK", "ETB", "EUR", "GHS", "HUF", "MMK", "NOK", "PLN", "RON")

# Орієнтовний вивід дешевою мережею (TRC20 для стейблкоїнів): біржа змінює його часто.
# Актив без запису в міжбіржовий арбітраж не потрапляє
WITHDRAWAL_FEES = {
    "USDT": Decimal("1.5"),
    "USDC": Decimal("1.5"),
    "BTC": Decimal("0.0005"),
    "ETH": Decimal("0.005"),
}

log = structlog.get_logger()


class BinanceAdapter(ExchangeAdapter):
    """Binance P2P (неофіційні публічні ендпоінти)."""

    name = "binance"
    base_url = BASE_URL

    async def list_markets(self) -> list[P2PMarket]:
        fiats = await self._post(FIAT_LIST_PATH, {})
        codes = sorted({str(item["currencyCode"]).upper() for item in fiats} | set(EXTRA_FIATS))
        markets: list[P2PMarket] = []
        for fiat in codes:
            try:
                config = await self._post(CONFIG_PATH, {"fiat": fiat})
                # Серед активів трапляється фіат (ARS/ARS, MXN/CLP) — це не крипто-пари
                parsed = self._parse_config(config, fiat)
                markets.extend(market for market in parsed if market.asset not in codes)
            except ExchangeBlocked:
                raise
            except ExchangeError as exc:
                # Збій одного фіату не скасовує весь довідник: він оновиться наступного разу
                log.warning("binance.market_config_failed", fiat=fiat, error=exc.detail)
        return markets

    async def fetch_offers(
        self,
        asset: str,
        fiat: str,
        side: Side,
        payment: str | None = None,
        page: int = 1,
    ) -> list[Offer]:
        # tradeType у Binance — з погляду користувача, як і наш side
        payload = {
            "asset": asset,
            "fiat": fiat,
            "tradeType": side,
            "page": page,
            "rows": PAGE_SIZE,
            "payTypes": [],
            "publisherType": None,
        }
        items = await self._post(SEARCH_PATH, payload)
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
        # Для звичайних користувачів P2P на Binance без комісії
        return FeeSchedule(exchange=cls.name, withdrawal_fees=WITHDRAWAL_FEES)

    def _is_blocked(self, response: httpx.Response) -> bool:
        # Капча WAF приходить як HTML і зі статусом 2xx
        is_html = "text/html" in response.headers.get("content-type", "")
        return super()._is_blocked(response) or is_html

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        response = await self._request("POST", path, json=payload)
        try:
            body = response.json(parse_float=Decimal)
        except ValueError as exc:
            raise ExchangeError(self.name, f"некоректна відповідь: {exc!r}") from exc
        if not isinstance(body, dict):
            raise ExchangeError(self.name, "некоректна відповідь: очікувався об'єкт")
        if body.get("code") != OK_CODE or body.get("data") is None:
            detail = f"помилка біржі: {body.get('code')} {body.get('message')}"
            raise ExchangeError(self.name, detail)
        return body["data"]

    def _parse_config(self, config: dict[str, Any], fiat: str) -> list[P2PMarket]:
        assets: set[str] = set()
        payments: dict[str, None] = {}
        for area in config.get("areas", []):
            if area.get("area") != P2P_AREA:
                continue
            for trade_side in area.get("tradeSides", []):
                assets.update(str(item["asset"]).upper() for item in trade_side.get("assets", []))
                for method in trade_side.get("tradeMethods", []):
                    payments[normalize_payment(method["identifier"])] = None
        return [
            P2PMarket(exchange=self.name, asset=asset, fiat=fiat, payment_methods=list(payments))
            for asset in sorted(assets)
        ]

    def _parse_item(self, item: dict[str, Any], side: Side, fetched_at: datetime) -> Offer:
        adv, advertiser = item["adv"], item["advertiser"]
        methods = adv.get("tradeMethods", [])
        payments = dict.fromkeys(normalize_payment(method["identifier"]) for method in methods)
        return Offer(
            exchange=self.name,
            side=side,
            asset=str(adv["asset"]).upper(),
            fiat=str(adv["fiatUnit"]).upper(),
            price=Decimal(adv["price"]),
            min_amount=Decimal(adv["minSingleTransAmount"]),
            # dynamicMax враховує залишок оголошення, maxSingle — лише заявлений ліміт
            max_amount=Decimal(
                adv.get("dynamicMaxSingleTransAmount") or adv["maxSingleTransAmount"]
            ),
            available=Decimal(adv["tradableQuantity"]),
            payment_methods=list(payments),
            merchant=Merchant(
                id=str(advertiser["userNo"]),
                nickname=str(advertiser.get("nickName", "")),
                orders_30d=int(advertiser.get("monthOrderCount", 0)),
                completion_rate=Decimal(advertiser.get("monthFinishRate", 0)),
            ),
            fetched_at=fetched_at,
            external_id=adv.get("advNo"),
        )
