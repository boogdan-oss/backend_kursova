from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Asset, CurrencyPair, Exchange, Fiat, Merchant, OfferSnapshot
from app.domain import Offer


async def _get_or_create_code(session: AsyncSession, model: type[Asset | Fiat], code: str) -> int:
    """Нові активи й фіати з'являються динамічно; назву згодом уточнить довідник бірж."""
    await session.execute(
        pg_insert(model)
        .values(code=code, name=code)
        .on_conflict_do_nothing(index_elements=["code"])
    )
    return (await session.execute(select(model.id).where(model.code == code))).scalar_one()


async def get_or_create_pair(session: AsyncSession, asset: str, fiat: str) -> int:
    asset_id = await _get_or_create_code(session, Asset, asset)
    fiat_id = await _get_or_create_code(session, Fiat, fiat)
    await session.execute(
        pg_insert(CurrencyPair)
        .values(base_currency_id=asset_id, quote_currency_id=fiat_id)
        .on_conflict_do_nothing(index_elements=["base_currency_id", "quote_currency_id"])
    )
    query = select(CurrencyPair.id).where(
        CurrencyPair.base_currency_id == asset_id, CurrencyPair.quote_currency_id == fiat_id
    )
    return (await session.execute(query)).scalar_one()


def merchant_rows(exchange_id: int, offers: Sequence[Offer]) -> list[dict[str, Any]]:
    """Один рядок на продавця: ON CONFLICT DO UPDATE не може змінити той самий рядок двічі."""
    merchants = {offer.merchant.id: offer.merchant for offer in offers}
    return [
        {
            "exchange_id": exchange_id,
            "external_id": merchant.id,
            "nickname": merchant.nickname,
            "orders_30d": merchant.orders_30d,
            "completion_rate": merchant.completion_rate,
        }
        for merchant in merchants.values()
    ]


async def upsert_merchants(
    session: AsyncSession, exchange_id: int, offers: Sequence[Offer]
) -> dict[str, int]:
    """Оновлює продавців і повертає відповідність external_id → id."""
    insert_stmt = pg_insert(Merchant).values(merchant_rows(exchange_id, offers))
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=["exchange_id", "external_id"],
        set_={
            "nickname": insert_stmt.excluded.nickname,
            "orders_30d": insert_stmt.excluded.orders_30d,
            "completion_rate": insert_stmt.excluded.completion_rate,
            "updated_at": func.now(),
        },
    ).returning(Merchant.external_id, Merchant.id)
    return {external_id: id_ for external_id, id_ in (await session.execute(upsert_stmt)).all()}


async def save_offers(
    session: AsyncSession, exchange: str, asset: str, fiat: str, offers: Sequence[Offer]
) -> int:
    """Зберігає знімок оголошень однієї пари на одній біржі. Коміт — на стороні сервісу."""
    if not offers:
        return 0

    query = select(Exchange.id).where(Exchange.code == exchange)
    exchange_id = (await session.execute(query)).scalar_one()
    pair_id = await get_or_create_pair(session, asset, fiat)
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
    await session.execute(
        update(Exchange).where(Exchange.id == exchange_id).values(last_ok_at=datetime.now(UTC))
    )
    return len(offers)
