from abc import ABC, abstractmethod
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.collectors.ratelimit import AdaptiveRateLimiter
from app.domain import FeeSchedule, Offer, P2PMarket, Side

BLOCK_STATUSES = frozenset({403, 418, 429})
HTTP_TIMEOUT_SEC = 10.0
RETRY_ATTEMPTS = 3
RETRY_BASE_SEC = 0.5
USER_AGENT = "Mozilla/5.0 (CryptoParser)"


class ExchangeError(Exception):
    """Біржа недоступна або повернула некоректну відповідь."""

    def __init__(self, exchange: str, detail: str) -> None:
        super().__init__(f"{exchange}: {detail}")
        self.exchange = exchange
        self.detail = detail


class ExchangeUnavailable(ExchangeError):
    """Тимчасовий збій мережі або сервера біржі — запит можна повторити."""


class ExchangeBlocked(ExchangeError):
    """Біржа обмежила запити (429 / 418 / 403 / капча). Паузу вже виставив rate limiter."""


def parse_retry_after(value: str | None) -> float | None:
    """Заголовок Retry-After: секунди або HTTP-дата."""
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        return max((parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds(), 0.0)
    except (TypeError, ValueError):
        return None


class ExchangeAdapter(ABC):
    """Уся специфіка біржі (URL, формат відповіді, назви банків) — лише в адаптері."""

    name: str
    base_url: str

    def __init__(
        self,
        limiter: AdaptiveRateLimiter,
        client: httpx.AsyncClient | None = None,
        retry_base_sec: float = RETRY_BASE_SEC,
    ) -> None:
        self.limiter = limiter
        self._retry_base_sec = retry_base_sec
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=HTTP_TIMEOUT_SEC,
            headers={"User-Agent": USER_AGENT},
        )

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

    @classmethod
    @abstractmethod
    def fees(cls) -> FeeSchedule:
        """Комісії біржі. Без екземпляра: API читає їх, не створюючи клієнтів бірж."""

    async def aclose(self) -> None:
        """Закрити HTTP-клієнт адаптера."""
        await self._client.aclose()

    def _is_blocked(self, response: httpx.Response) -> bool:
        return response.status_code in BLOCK_STATUSES

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Єдиний вихід на біржу: кожна спроба йде через rate limiter.

        Тимчасові збої повторюються з експоненційною паузою. Блокування не повторюються —
        ними керує rate limiter, інакше ретраї лише продовжували б бан.
        """
        retrying = AsyncRetrying(
            retry=retry_if_exception_type(ExchangeUnavailable),
            stop=stop_after_attempt(RETRY_ATTEMPTS),
            wait=wait_exponential(multiplier=self._retry_base_sec),
            reraise=True,
        )
        return await retrying(self._send, method, url, **kwargs)

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        await self.limiter.acquire()
        try:
            response = await self._client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            self.limiter.on_error()
            raise ExchangeUnavailable(self.name, f"запит не вдався: {exc!r}") from exc

        if self._is_blocked(response):
            self.limiter.on_blocked(parse_retry_after(response.headers.get("Retry-After")))
            raise ExchangeBlocked(self.name, f"біржа обмежила запити: {response.status_code}")
        if response.is_server_error:
            self.limiter.on_error()
            raise ExchangeUnavailable(self.name, f"помилка сервера: {response.status_code}")
        if response.is_error:
            self.limiter.on_error()
            raise ExchangeError(self.name, f"запит відхилено: {response.status_code}")

        self.limiter.on_success()
        return response
