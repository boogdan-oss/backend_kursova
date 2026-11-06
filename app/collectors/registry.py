from app.collectors.base import ExchangeAdapter
from app.collectors.binance import BinanceAdapter
from app.collectors.bybit import BybitAdapter
from app.collectors.ratelimit import AdaptiveRateLimiter
from app.core.config import get_settings
from app.domain import FeeSchedule

# Нова біржа = новий клас адаптера + рядок тут
ADAPTER_CLASSES: dict[str, type[ExchangeAdapter]] = {
    BinanceAdapter.name: BinanceAdapter,
    BybitAdapter.name: BybitAdapter,
}

_instances: dict[str, ExchangeAdapter] = {}


def available_exchanges() -> list[str]:
    return list(ADAPTER_CLASSES)


def exchange_fees() -> dict[str, FeeSchedule]:
    return {name: adapter.fees() for name, adapter in ADAPTER_CLASSES.items()}


def get_adapter(name: str) -> ExchangeAdapter:
    """Один екземпляр (один HTTP-клієнт і один rate limiter) на біржу."""
    if name not in _instances:
        settings = get_settings()
        limiter = AdaptiveRateLimiter(
            settings.rate_limit_initial_rps,
            settings.rate_limit_max_rps,
            settings.rate_limit_cooldown_sec,
        )
        _instances[name] = ADAPTER_CLASSES[name](limiter)
    return _instances[name]


async def aclose_adapters() -> None:
    for adapter in _instances.values():
        await adapter.aclose()
    _instances.clear()
