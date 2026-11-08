from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.collectors.base import APPROXIMATE_WITHDRAWAL_FEES, ExchangeAdapter, ExchangeError
from app.collectors.payment_map import normalize_payment
from app.domain import FeeSchedule, Merchant, Offer, P2PMarket, Side

BASE_URL = "https://www.bitget.com"
ADS_PATH = "/v1/p2p/pub/adv/queryAdvList"
CONFIG_PATH = "/v1/p2p/pub/currency/queryAllCoinAndFiat"
PAGE_SIZE = 20
OK_CODE = "00000"
SIDES: dict[Side, int] = {"BUY": 1, "SELL": 2}


def _decimal(value: Any) -> Decimal:
    return Decimal(value) if value not in (None, "") else Decimal(0)


class BitgetAdapter(ExchangeAdapter):
    """Bitget P2P (неофіційні публічні ендпоінти)."""

    name = "bitget"
    base_url = BASE_URL

    async def list_markets(self) -> list[P2PMarket]:
        """Списку пар Bitget не віддає, лише монети й фіати: пари — усі їхні поєднання."""
        config = await self._post(CONFIG_PATH, {"languageType": 0})
        coins = [str(coin["coinCode"]).upper() for coin in config["coinInfoRespList"]]
        markets: list[P2PMarket] = []
        for fiat in config["fiatInfoRespList"]:
            methods = fiat.get("paymethodInfo") or []
            payments = dict.fromkeys(normalize_payment(item["paymethodName"]) for item in methods)
            markets.extend(
                P2PMarket(
                    exchange=self.name,
                    asset=coin,
                    fiat=str(fiat["fiatCode"]).upper(),
                    payment_methods=list(payments),
                )
                for coin in coins
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
        payload = {
            "side": SIDES[side],
            "pageNo": page,
            "pageSize": PAGE_SIZE,
            "coinCode": asset,
            "fiatCode": fiat,
            "languageType": 0,
        }
        data = await self._post(ADS_PATH, payload)
        fetched_at = datetime.now(UTC)
        try:
            offers = [
                self._parse_item(item, side, fetched_at) for item in data.get("dataList") or []
            ]
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise ExchangeError(self.name, f"формат відповіді змінився: {exc!r}") from exc
        if payment is not None:
            key = normalize_payment(payment)
            offers = [offer for offer in offers if key in offer.payment_methods]
        return offers

    @classmethod
    def fees(cls) -> FeeSchedule:
        return FeeSchedule(exchange=cls.name, withdrawal_fees=APPROXIMATE_WITHDRAWAL_FEES)

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        response = await self._request("POST", path, json=payload)
        try:
            body = response.json(parse_float=Decimal)
        except ValueError as exc:
            raise ExchangeError(self.name, f"некоректна відповідь: {exc!r}") from exc
        if not isinstance(body, dict):
            raise ExchangeError(self.name, "некоректна відповідь: очікувався об'єкт")
        if body.get("code") != OK_CODE or body.get("data") is None:
            raise ExchangeError(self.name, f"помилка біржі: {body.get('code')} {body.get('msg')}")
        return body["data"]

    def _parse_item(self, item: dict[str, Any], side: Side, fetched_at: datetime) -> Offer:
        price = Decimal(item["price"])
        available = Decimal(item["lastAmount"])
        methods = item.get("paymethodInfo") or []
        payments = dict.fromkeys(normalize_payment(method["paymethodName"]) for method in methods)
        return Offer(
            exchange=self.name,
            side=side,
            asset=str(item["coinCode"]).upper(),
            fiat=str(item["fiatCode"]).upper(),
            price=price,
            min_amount=Decimal(item["minAmount"]),
            max_amount=min(Decimal(item["maxAmount"]), available * price),
            available=available,
            payment_methods=list(payments),
            merchant=Merchant(
                id=str(item["encryptUserId"]),
                nickname=str(item.get("nickName", "")).strip(),
                orders_30d=int(_decimal(item.get("thirtyTunoverNum"))),
                completion_rate=_decimal(item.get("thirtyCompletionRate")),
            ),
            fetched_at=fetched_at,
            external_id=str(item["adNo"]),
        )
