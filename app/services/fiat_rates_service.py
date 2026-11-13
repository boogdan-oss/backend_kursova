"""Офіційні курси валют НБУ для AI-асистента (відкритий API bank.gov.ua, без ключа).

Курс НБУ — гривень за одиницю валюти; крос-курс до іншої бази рахується через гривню.
Відповіді кешуються в пам'яті процесу: за сьогодні — на 10 хвилин, за минулі дати — на добу.
"""

import json
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import httpx

NBU_URL = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange"
TODAY_TTL_SEC = 600
PAST_TTL_SEC = 86400
CACHE_SIZE = 32
TIMEOUT_SEC = 10.0


class RatesUnavailable(Exception):
    """НБУ не відповів або відповів не тим."""


@dataclass(frozen=True, slots=True)
class OfficialRates:
    day: date
    uah_per_unit: dict[str, Decimal]
    names: dict[str, str]


_cache: dict[date | None, tuple[float, OfficialRates]] = {}


def _parse(payload: Any) -> OfficialRates:
    if not isinstance(payload, list) or not payload:
        raise RatesUnavailable("НБУ не повернув курсів на цю дату")
    rates: dict[str, Decimal] = {}
    names: dict[str, str] = {}
    day: date | None = None
    for row in payload:
        code, rate = row.get("cc"), row.get("rate")
        if not isinstance(code, str) or rate is None:
            continue
        rates[code.upper()] = Decimal(str(rate))
        names[code.upper()] = str(row.get("txt") or code)
        if day is None and row.get("exchangedate"):
            day = datetime.strptime(row["exchangedate"], "%d.%m.%Y").date()
    if not rates or day is None:
        raise RatesUnavailable("НБУ не повернув курсів на цю дату")
    return OfficialRates(day, rates, names)


async def official_rates(
    day: date | None = None, client: httpx.AsyncClient | None = None
) -> OfficialRates:
    """Курси НБУ на дату; без дати — на сьогодні."""
    cached = _cache.get(day)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]
    params = {"json": ""} | ({"date": day.strftime("%Y%m%d")} if day else {})
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=TIMEOUT_SEC) as own:
                response = await own.get(NBU_URL, params=params)
        else:
            response = await client.get(NBU_URL, params=params)
        response.raise_for_status()
        rates = _parse(json.loads(response.text, parse_float=Decimal))
    except (httpx.HTTPError, ValueError) as exc:
        raise RatesUnavailable("Сервіс курсів НБУ зараз недоступний") from exc
    if len(_cache) >= CACHE_SIZE:
        _cache.clear()
    ttl = TODAY_TTL_SEC if day is None or day >= date.today() else PAST_TTL_SEC
    _cache[day] = (time.monotonic() + ttl, rates)
    return rates


def cross_rate(rates: OfficialRates, currency: str, base: str) -> Decimal | None:
    """Скільки base коштує одиниця currency за курсом НБУ; None — валюти немає в НБУ."""
    uah = {**rates.uah_per_unit, "UAH": Decimal(1)}
    if currency not in uah or base not in uah:
        return None
    return uah[currency] / uah[base]
