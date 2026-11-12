"""Watchlist, правила сповіщень і самі сповіщення."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVarTuple

from sqlalchemy import Select, and_, delete, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AlertRule,
    Asset,
    CurrencyPair,
    Exchange,
    Fiat,
    Notification,
    User,
    UserStatus,
    WatchlistItem,
)
from app.domain import AlertRule as RuleModel


@dataclass(frozen=True, slots=True)
class WatchlistRow:
    id: int
    asset: str
    fiat: str
    exchange: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RuleRow:
    rule: RuleModel
    created_at: datetime
    last_triggered_at: datetime | None
    triggered_today: int


@dataclass(frozen=True, slots=True)
class RuleTarget:
    """Правило разом з тим, куди його доставляти."""

    rule: RuleModel
    username: str
    email: str
    telegram_chat_id: str | None


Columns = TypeVarTuple("Columns")


def _with_codes(query: Select[*Columns]) -> Select[*Columns]:
    return (
        query.join(CurrencyPair, CurrencyPair.id == AlertRule.pair_id)
        .join(Asset, Asset.id == CurrencyPair.base_currency_id)
        .join(Fiat, Fiat.id == CurrencyPair.quote_currency_id)
        .outerjoin(Exchange, Exchange.id == AlertRule.exchange_id)
    )


def _to_rule(row: AlertRule, asset: str, fiat: str, exchange: str | None) -> RuleModel:
    return RuleModel(
        id=row.id,
        user_id=row.user_id,
        asset=asset,
        fiat=fiat,
        exchange=exchange,
        payment=row.payment,
        title=row.title,
        min_spread=row.min_spread,
        min_net_spread=row.min_net_spread,
        min_ai_score=row.min_ai_score,
        min_rr=row.min_rr,
        min_net_profit=row.min_net_profit,
        channels=row.channels,
        enabled=row.enabled,
    )


async def list_watchlist(session: AsyncSession, user_id: int) -> list[WatchlistRow]:
    query = (
        select(WatchlistItem.id, Asset.code, Fiat.code, Exchange.code, WatchlistItem.created_at)
        .join(CurrencyPair, CurrencyPair.id == WatchlistItem.pair_id)
        .join(Asset, Asset.id == CurrencyPair.base_currency_id)
        .join(Fiat, Fiat.id == CurrencyPair.quote_currency_id)
        .outerjoin(Exchange, Exchange.id == WatchlistItem.exchange_id)
        .where(WatchlistItem.user_id == user_id)
        .order_by(WatchlistItem.created_at, WatchlistItem.id)
    )
    return [WatchlistRow(*row) for row in (await session.execute(query)).all()]


async def count_watchlist(session: AsyncSession, user_id: int) -> int:
    query = select(func.count()).where(WatchlistItem.user_id == user_id)
    return (await session.execute(query)).scalar_one()


async def add_watchlist(
    session: AsyncSession, user_id: int, pair_id: int, exchange_id: int | None
) -> tuple[int, bool]:
    """Повертає id і чи запис новий: та сама пара з тією ж біржею не дублюється."""
    inserted = await session.execute(
        pg_insert(WatchlistItem)
        .values(user_id=user_id, pair_id=pair_id, exchange_id=exchange_id)
        .on_conflict_do_nothing()
        .returning(WatchlistItem.id)
    )
    item_id = inserted.scalar_one_or_none()
    if item_id is not None:
        return item_id, True
    existing = await session.execute(
        select(WatchlistItem.id).where(
            WatchlistItem.user_id == user_id,
            WatchlistItem.pair_id == pair_id,
            WatchlistItem.exchange_id.is_(None)
            if exchange_id is None
            else WatchlistItem.exchange_id == exchange_id,
        )
    )
    return existing.scalar_one(), False


async def delete_watchlist(session: AsyncSession, user_id: int, item_id: int) -> bool:
    result = await session.execute(
        delete(WatchlistItem)
        .where(WatchlistItem.id == item_id, WatchlistItem.user_id == user_id)
        .returning(WatchlistItem.id)
    )
    return result.first() is not None


async def list_rules(session: AsyncSession, user_id: int, today: datetime) -> list[RuleRow]:
    stats = (
        select(
            Notification.alert_rule_id.label("rule_id"),
            func.max(Notification.sent_at).label("last"),
            func.count().filter(Notification.sent_at >= today).label("today"),
        )
        .where(Notification.user_id == user_id)
        .group_by(Notification.alert_rule_id)
        .subquery()
    )
    query = (
        _with_codes(
            select(AlertRule, Asset.code, Fiat.code, Exchange.code, stats.c.last, stats.c.today)
        )
        .outerjoin(stats, stats.c.rule_id == AlertRule.id)
        .where(AlertRule.user_id == user_id)
        .order_by(AlertRule.created_at, AlertRule.id)
    )
    return [
        RuleRow(_to_rule(rule, asset, fiat, exchange), rule.created_at, last, today or 0)
        for rule, asset, fiat, exchange, last, today in (await session.execute(query)).all()
    ]


async def count_rules(session: AsyncSession, user_id: int) -> int:
    query = select(func.count()).where(AlertRule.user_id == user_id)
    return (await session.execute(query)).scalar_one()


async def create_rule(session: AsyncSession, values: dict[str, Any]) -> int:
    rule = AlertRule(**values)
    session.add(rule)
    await session.flush()
    return rule.id


async def update_rule(
    session: AsyncSession, user_id: int, rule_id: int, values: dict[str, Any]
) -> bool:
    if not values:
        query = select(AlertRule.id).where(AlertRule.id == rule_id, AlertRule.user_id == user_id)
        return (await session.execute(query)).first() is not None
    result = await session.execute(
        update(AlertRule)
        .where(AlertRule.id == rule_id, AlertRule.user_id == user_id)
        .values(**values)
        .returning(AlertRule.id)
    )
    return result.first() is not None


async def delete_rule(session: AsyncSession, user_id: int, rule_id: int) -> bool:
    result = await session.execute(
        delete(AlertRule)
        .where(AlertRule.id == rule_id, AlertRule.user_id == user_id)
        .returning(AlertRule.id)
    )
    return result.first() is not None


async def get_rule(session: AsyncSession, user_id: int, rule_id: int) -> RuleModel | None:
    query = _with_codes(select(AlertRule, Asset.code, Fiat.code, Exchange.code)).where(
        AlertRule.id == rule_id, AlertRule.user_id == user_id
    )
    row = (await session.execute(query)).first()
    return None if row is None else _to_rule(*row)


async def rules_for_pairs(
    session: AsyncSession, pairs: Iterable[tuple[str, str]]
) -> list[RuleTarget]:
    """Увімкнені правила активних користувачів на ці пари."""
    pairs = list(pairs)
    if not pairs:
        return []
    query = (
        _with_codes(
            select(AlertRule, Asset.code, Fiat.code, Exchange.code, User.username, User.email)
        )
        .join(User, User.id == AlertRule.user_id)
        .add_columns(User.telegram_chat_id)
        .where(
            AlertRule.enabled,
            User.status == UserStatus.ACTIVE,
            tuple_(Asset.code, Fiat.code).in_(pairs),
        )
        .order_by(AlertRule.id)
    )
    return [
        RuleTarget(_to_rule(rule, asset, fiat, exchange), username, email, chat_id)
        for rule, asset, fiat, exchange, username, email, chat_id in (
            await session.execute(query)
        ).all()
    ]


async def create_notification(
    session: AsyncSession, values: dict[str, Any]
) -> tuple[int, datetime]:
    notification = Notification(**values)
    session.add(notification)
    await session.flush()
    await session.refresh(notification, ["sent_at"])
    return notification.id, notification.sent_at


async def set_delivery(
    session: AsyncSession, notification_id: int, delivery: dict[str, str]
) -> None:
    await session.execute(
        update(Notification).where(Notification.id == notification_id).values(delivery=delivery)
    )


async def list_notifications(
    session: AsyncSession, user_id: int, unread_only: bool, limit: int
) -> tuple[list[tuple[Notification, str | None]], int, int]:
    """Сповіщення (новіші першими) з назвою правила, загальна кількість і непрочитані."""
    base = Notification.user_id == user_id
    query = (
        select(Notification, AlertRule.title)
        .outerjoin(AlertRule, AlertRule.id == Notification.alert_rule_id)
        .where(base, ~Notification.read if unread_only else and_(True))
        .order_by(Notification.sent_at.desc(), Notification.id.desc())
        .limit(limit)
    )
    rows = [(row, title) for row, title in (await session.execute(query)).all()]
    counts = await session.execute(
        select(func.count(), func.count().filter(~Notification.read)).where(base)
    )
    total, unread = counts.one()
    return rows, total, unread


async def mark_read(session: AsyncSession, user_id: int, ids: list[int] | None) -> int:
    condition = [Notification.user_id == user_id, ~Notification.read]
    if ids is not None:
        condition.append(Notification.id.in_(ids))
    result = await session.execute(
        update(Notification).where(*condition).values(read=True).returning(Notification.id)
    )
    return len(result.all())
