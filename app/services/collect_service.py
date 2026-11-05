from app.collectors.registry import get_adapter
from app.db.repositories.offers import save_offers
from app.db.session import SessionLocal


async def collect_pair(exchange: str, asset: str, fiat: str) -> int:
    """Один цикл збору пари: обидві сторони з біржі → offer_snapshots. Повертає кількість рядків.

    Частоту викликів визначатимуть rate limiter і планувальник collector (CLAUDE.md, розділ 5.1).
    """
    adapter = get_adapter(exchange)
    offers = [
        *await adapter.fetch_offers(asset, fiat, "BUY"),
        *await adapter.fetch_offers(asset, fiat, "SELL"),
    ]
    async with SessionLocal() as session, session.begin():
        return await save_offers(session, exchange, asset, fiat, offers)
