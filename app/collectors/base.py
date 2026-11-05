from abc import ABC, abstractmethod

from app.domain import FeeSchedule, Offer, P2PMarket, Side


class ExchangeError(Exception):
    """Біржа недоступна або повернула некоректну відповідь."""

    def __init__(self, exchange: str, detail: str) -> None:
        super().__init__(f"{exchange}: {detail}")
        self.exchange = exchange
        self.detail = detail


class ExchangeAdapter(ABC):
    """Уся специфіка біржі (URL, формат відповіді, назви банків) — лише в адаптері."""

    name: str

    @abstractmethod
    async def list_markets(self) -> list[P2PMarket]:
        """Підтримувані пари (asset, fiat) і способи оплати для кожного фіату."""

    @abstractmethod
    async def fetch_offers(
        self,
        asset: str,
        fiat: str,
        side: Side,
        payment: str | None = None,
        page: int = 1,
    ) -> list[Offer]:
        """Оголошення однієї сторони, від найвигіднішого для користувача."""

    @abstractmethod
    def fees(self) -> FeeSchedule: ...

    @abstractmethod
    async def aclose(self) -> None:
        """Закрити HTTP-клієнт адаптера."""
