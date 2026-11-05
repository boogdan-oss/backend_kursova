from app.collectors.base import ExchangeAdapter
from app.collectors.binance import BinanceAdapter

# Нова біржа = новий клас адаптера + рядок тут
ADAPTER_CLASSES: dict[str, type[ExchangeAdapter]] = {
    BinanceAdapter.name: BinanceAdapter,
}

_instances: dict[str, ExchangeAdapter] = {}


def available_exchanges() -> list[str]:
    return list(ADAPTER_CLASSES)


def get_adapter(name: str) -> ExchangeAdapter:
    """Один екземпляр (і один HTTP-клієнт) на біржу."""
    if name not in _instances:
        _instances[name] = ADAPTER_CLASSES[name]()
    return _instances[name]


async def aclose_adapters() -> None:
    for adapter in _instances.values():
        await adapter.aclose()
    _instances.clear()
