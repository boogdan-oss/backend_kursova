"""Спільне для тестів адаптерів: фікстури й адаптер без затримок."""

import json
from pathlib import Path
from typing import Any

import httpx

from app.collectors.base import ExchangeAdapter
from app.collectors.ratelimit import AdaptiveRateLimiter

FIXTURES = Path(__file__).parent.parent / "fixtures"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_adapter(adapter_class: type[ExchangeAdapter], client: httpx.AsyncClient) -> Any:
    limiter = AdaptiveRateLimiter(initial_rps=1000, max_rps=1000, cooldown_sec=60)
    return adapter_class(limiter, client, retry_base_sec=0)  # type: ignore[call-arg]
