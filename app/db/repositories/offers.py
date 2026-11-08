from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import func, insert, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Merchant, OfferSnapshot
from app.db.repositories.market import MarketRow
from app.domain import Merchant as MerchantInfo
from app.domain import Offer, Side


def merchant_rows(exchange_id: int, offers: Sequence[Offer]) -> list[dict[str, Any]]:
    """Один рядок на продавця: ON CONFLICT DO UPDATE не може змінити той самий рядок двічі.

    Порядок за external_id: паралельні транзакції блокують рядки однаково — без deadlock.
    """
    merchants = {offer.merchant.id: offer.merchant for offer in offers}
    return [
        {
            "exchange_id": exchange_id,
            "external_id": merchant.id,
            "nickname": merchant.nickname,
            "orders_30d": merchant.orders_30d,
            "completion_rate": merchant.completion_rate,
        }
        for _, merchant in sorted(merchants.items())
    ]


async def upsert_merchants(
    session: AsyncSession, exchange_id: int, offers: Sequence[Offer]
) -> dict[str, int]:
    """Записує нових і змінених продавців і повертає відповідність external_id → id.

    Незмінних не чіпає: кожен INSERT ... ON CONFLICT витрачає значення послідовності
    merchants.id, і за постійного збору вона вичерпалась би за кілька місяців.
    """
    rows = merchant_rows(exchange_id, offers)
    query = select(
        Merchant.external_id,
        Merchant.id,
        Merchant.nickname,
        Merchant.orders_30d,
        Merchant.completion_rate,
    ).where(
        Merchant.exchange_id == exchange_id,
        Merchant.external_id.in_([row["external_id"] for row in rows]),
    )
    known = {row.external_id: row for row in (await session.execute(query)).all()}
    ids = {external_id: row.id for external_id, row in known.items()}

    changed = []
    for row in rows:
        current = known.get(row["external_id"])
        state = (row["nickname"], row["orders_30d"], row["completion_rate"])
        if current is None or state != tuple(current[2:]):
            changed.append(row)
    if not changed:
        return ids

    insert_stmt = pg_insert(Merchant).values(changed)
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=["exchange_id", "external_id"],
        set_={
            "nickname": insert_stmt.excluded.nickname,
            "orders_30d": insert_stmt.excluded.orders_30d,
            "completion_rate": insert_stmt.excluded.completion_rate,
            "updated_at": func.now(),
        },
    ).returning(Merchant.external_id, Merchant.id)
    result = await session.execute(upsert_stmt)
    ids.update({external_id: id_ for external_id, id_ in result.all()})
    return ids


async def save_offers(
    session: AsyncSession, exchange_id: int, pair_id: int, offers: Sequence[Offer]
) -> int:
    """Зберігає знімок оголошень однієї сторони пари. Коміт — на стороні сервісу."""
    if not offers:
        return 0

    merchant_ids = await upsert_merchants(session, exchange_id, offers)
    await session.execute(
        insert(OfferSnapshot),
        [
            {
                "time": offer.fetched_at,
                "exchange_id": exchange_id,
                "pair_id": pair_id,
                "side": offer.side,
                "price": offer.price,
                "min_amount": offer.min_amount,
                "max_amount": offer.max_amount,
                "available": offer.available,
                "payment_methods": offer.payment_methods,
                "merchant_id": merchant_ids[offer.merchant.id],
                "external_id": offer.external_id,
            }
            for offer in offers
        ],
    )
    return len(offers)


def _plain(value: Decimal) -> Decimal:
    """Numeric(28, 8) повертає 46.10000000 — у відповіді API потрібно 46.1."""
    return Decimal(format(value.normalize(), "f"))


def _to_offer(market: MarketRow, snapshot: OfferSnapshot, merchant: Merchant) -> Offer:
    return Offer(
        exchange=market.exchange,
        side=cast(Side, snapshot.side.value),
        asset=market.asset,
        fiat=market.fiat,
        price=_plain(snapshot.price),
        min_amount=_plain(snapshot.min_amount),
        max_amount=_plain(snapshot.max_amount),
        available=_plain(snapshot.available),
        payment_methods=snapshot.payment_methods,
        merchant=MerchantInfo(
            id=merchant.external_id,
            nickname=merchant.nickname,
            orders_30d=merchant.orders_30d,
            completion_rate=_plain(merchant.completion_rate),
        ),
        fetched_at=snapshot.time,
        external_id=snapshot.external_id,
    )


async def latest_offers(session: AsyncSession, market: MarketRow, side: Side) -> list[Offer]:
    """Актуальний пакет оголошень сторони — за вказівником з exchange_markets."""
    fetched_at = market.fetched_at(side)
    if fetched_at is None:
        return []
    query = (
        select(OfferSnapshot, Merchant)
        .join(Merchant, Merchant.id == OfferSnapshot.merchant_id)
        .where(
            OfferSnapshot.pair_id == market.pair_id,
            OfferSnapshot.side == side,
            OfferSnapshot.exchange_id == market.exchange_id,
            OfferSnapshot.time == fetched_at,
        )
    )
    rows = (await session.execute(query)).all()
    return [_to_offer(market, snapshot, merchant) for snapshot, merchant in rows]


async def fresh_offers(
    session: AsyncSession, markets: Sequence[MarketRow], since: datetime
) -> list[Offer]:
    """Останні пакети багатьох пар одним запитом; пакети, старші за since, не беруться."""
    batches = [
        (market.exchange_id, market.pair_id, side, fetched_at)
        for market in markets
        for side, fetched_at in (("BUY", market.buy_fetched_at), ("SELL", market.sell_fetched_at))
        if fetched_at is not None and fetched_at >= since
    ]
    if not batches:
        return []

    by_ids = {(market.exchange_id, market.pair_id): market for market in markets}
    key = tuple_(
        OfferSnapshot.exchange_id, OfferSnapshot.pair_id, OfferSnapshot.side, OfferSnapshot.time
    )
    query = (
        select(OfferSnapshot, Merchant)
        .join(Merchant, Merchant.id == OfferSnapshot.merchant_id)
        .where(OfferSnapshot.time >= since, key.in_(batches))
    )
    return [
        _to_offer(by_ids[snapshot.exchange_id, snapshot.pair_id], snapshot, merchant)
        for snapshot, merchant in (await session.execute(query)).all()
    ]
