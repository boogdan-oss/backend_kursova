from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from statistics import median

from app.domain import MarketSnapshot, Offer
from app.engine.filters import filter_by_payment, filter_reliable
from app.engine.spreads import PERCENT_PRECISION, round_money, spread_pct

TOP_OFFERS = 5


@dataclass(frozen=True, slots=True)
class Fill:
    """Результат проходження рівнів однієї сторони (FR-14, FR-15)."""

    quantity: Decimal
    average_price: Decimal
    best_price: Decimal
    slippage_pct: Decimal
    complete: bool


def _best_first(offers: list[Offer]) -> list[Offer]:
    return sorted(offers, key=lambda offer: offer.price, reverse=offers[0].side == "SELL")


def effective_price(offers: list[Offer]) -> Decimal | None:
    """Медіана топ-5 найвигідніших оголошень однієї сторони: захист від фейкових лотів."""
    if not offers:
        return None
    return median(offer.price for offer in _best_first(offers)[:TOP_OFFERS])


def typical_amount(offers: list[Offer]) -> Decimal | None:
    """Типова сума угоди — медіана мінімальних лімітів. Не залежить від валюти."""
    return median(offer.min_amount for offer in offers) if offers else None


def quantity_for(investment: Decimal, buy_price: Decimal) -> Decimal:
    """(1.4) Q = Investment / P_buy."""
    return investment / buy_price


def average_price(levels: Sequence[tuple[Decimal, Decimal]]) -> Decimal:
    """(1.7) P_avg = Σ(p_i × q_i) / Σq_i. Рівні — пари (ціна, кількість)."""
    if not levels:
        raise ValueError("Немає рівнів для розрахунку середньої ціни")
    return sum((price * quantity for price, quantity in levels), Decimal(0)) / sum(
        (quantity for _, quantity in levels), Decimal(0)
    )


def slippage_pct(average: Decimal, best: Decimal) -> Decimal:
    """(1.8) Slippage = (P_avg − P_best) / P_best × 100 %."""
    return (average - best) / best * 100


def fill_order(offers: list[Offer], quantity: Decimal) -> Fill | None:
    """Проходить оголошення від найкращого, доки не набере quantity.

    Рівень — оголошення: доступний обсяг обмежений і залишком, і лімітом угоди у фіаті.
    None — жодне оголошення не приймає таку суму.
    """
    remaining = quantity
    levels: list[tuple[Decimal, Decimal]] = []
    for offer in _best_first(offers) if offers else []:
        capacity = min(offer.available, offer.max_amount / offer.price)
        take = min(remaining, capacity)
        if take <= 0 or take * offer.price < offer.min_amount:
            continue
        levels.append((offer.price, take))
        remaining -= take
        if remaining <= 0:
            break
    if not levels:
        return None

    average = round_money(average_price(levels))
    best = levels[0][0]
    return Fill(
        quantity=round_money(quantity - remaining),
        average_price=average,
        best_price=best,
        slippage_pct=abs(slippage_pct(average, best)).quantize(PERCENT_PRECISION),
        complete=remaining <= 0,
    )


def market_snapshot(
    exchange: str,
    asset: str,
    fiat: str,
    buy_offers: list[Offer],
    sell_offers: list[Offer],
    fetched_at: datetime,
    payment_method: str | None = None,
) -> MarketSnapshot:
    """Ефективні ціни пари на біржі. Ціна — лише за надійними продавцями, обсяг — за всіма.

    Обсяг і ліміти окремо по сторонах — за надійними: з них ланцюжки перевіряють, чи
    вкладається сума кожного кроку в оголошення.
    """
    reliable_buy, reliable_sell = filter_reliable(buy_offers), filter_reliable(sell_offers)
    buy_price = effective_price(reliable_buy)
    sell_price = effective_price(reliable_sell)
    spread = None
    if buy_price is not None and sell_price is not None:
        spread = spread_pct(buy_price, sell_price)
    return MarketSnapshot(
        exchange=exchange,
        asset=asset,
        fiat=fiat,
        payment_method=payment_method,
        buy_price=buy_price,
        sell_price=sell_price,
        spread_pct=spread,
        volume=sum((offer.available for offer in [*buy_offers, *sell_offers]), Decimal(0)),
        offers_count_buy=len(buy_offers),
        offers_count_sell=len(sell_offers),
        buy_volume=_available(reliable_buy),
        sell_volume=_available(reliable_sell),
        buy_min_amount=min((offer.min_amount for offer in reliable_buy), default=None),
        buy_max_amount=max((offer.max_amount for offer in reliable_buy), default=None),
        sell_min_amount=min((offer.min_amount for offer in reliable_sell), default=None),
        sell_max_amount=max((offer.max_amount for offer in reliable_sell), default=None),
        fetched_at=fetched_at,
    )


def _available(offers: list[Offer]) -> Decimal | None:
    return sum((offer.available for offer in offers), Decimal(0)) if offers else None


def exchange_snapshots(
    exchange: str,
    asset: str,
    fiat: str,
    buy_offers: list[Offer],
    sell_offers: list[Offer],
    fetched_at: datetime,
) -> list[MarketSnapshot]:
    """Знімок пари на біржі: спершу по всіх способах оплати, далі — по кожному окремо."""
    snapshots = [market_snapshot(exchange, asset, fiat, buy_offers, sell_offers, fetched_at)]
    offers = [*buy_offers, *sell_offers]
    payments = sorted({key for offer in offers for key in offer.payment_methods})
    for payment in payments:
        snapshots.append(
            market_snapshot(
                exchange,
                asset,
                fiat,
                filter_by_payment(buy_offers, payment),
                filter_by_payment(sell_offers, payment),
                fetched_at,
                payment_method=payment,
            )
        )
    return snapshots
