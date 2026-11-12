"""Smart Alerts (FR-16) і Watchlist (FR-17): правила користувача і доставка сповіщень.

API керує правилами й обраним; collector після кожного проходу сканера передає нові
можливості в AlertDispatcher. Сповіщення пишеться в БД і йде власнику подією alert одразу,
а Telegram і пошта — у фоні, щоб повільна доставка не гальмувала збір.
"""

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collectors.payment_map import normalize_payment
from app.core.config import get_settings
from app.db.models import User
from app.db.repositories.alerts import (
    RuleRow,
    RuleTarget,
    WatchlistRow,
    add_watchlist,
    count_rules,
    count_watchlist,
    create_notification,
    create_rule,
    delete_rule,
    delete_watchlist,
    get_rule,
    list_rules,
    list_watchlist,
    rules_for_pairs,
    set_delivery,
    update_rule,
)
from app.db.repositories.market import find_markets
from app.db.session import SessionLocal
from app.domain import AlertEvent, AlertNotification, AlertRule, NotificationChannel, Opportunity
from app.engine.alerts import has_threshold, rule_matches
from app.pubsub import get_redis, publish_event
from app.services.email_service import EmailError, email_enabled, send_email
from app.services.market_service import PairNotFound
from app.services.telegram_service import TelegramClient, TelegramError

MAX_RULES = 50
MAX_WATCHLIST = 50
MAX_EXTERNAL_PER_USER = 5
THRESHOLD_FIELDS = ("min_spread", "min_net_spread", "min_ai_score", "min_rr", "min_net_profit")

log = structlog.get_logger()


class AlertError(Exception):
    """Правило або обране не можна зберегти; текст — для користувача."""


class LimitReached(AlertError):
    pass


class RuleNotFound(AlertError):
    pass


async def _resolve_pair(
    session: AsyncSession, asset: str, fiat: str, exchange: str | None
) -> tuple[int, int | None]:
    markets = await find_markets(session, exchange, asset, fiat)
    if not markets:
        raise PairNotFound(asset, fiat)
    return markets[0].pair_id, markets[0].exchange_id if exchange is not None else None


def _check_channels(user: User, channels: list[NotificationChannel]) -> None:
    if "telegram" in channels and not user.telegram_chat_id:
        raise AlertError("Спершу підключіть Telegram у профілі")
    if "email" in channels and not email_enabled():
        raise AlertError("Сповіщення поштою на сервері не налаштовані")


def start_of_today() -> datetime:
    """Початок доби за часом застосунку (APP_TIMEZONE) для лічильника «сьогодні»."""
    zone = ZoneInfo(get_settings().app_timezone)
    return datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)


async def get_watchlist(session: AsyncSession, user: User) -> list[WatchlistRow]:
    return await list_watchlist(session, user.id)


async def add_to_watchlist(
    session: AsyncSession, user: User, asset: str, fiat: str, exchange: str | None
) -> tuple[WatchlistRow, bool]:
    pair_id, exchange_id = await _resolve_pair(session, asset, fiat, exchange)
    item_id, created = await add_watchlist(session, user.id, pair_id, exchange_id)
    if created and await count_watchlist(session, user.id) > MAX_WATCHLIST:
        await session.rollback()
        raise LimitReached(f"В обраному може бути не більше {MAX_WATCHLIST} пар")
    await session.commit()
    rows = await list_watchlist(session, user.id)
    return next(row for row in rows if row.id == item_id), created


async def remove_from_watchlist(session: AsyncSession, user: User, item_id: int) -> bool:
    removed = await delete_watchlist(session, user.id, item_id)
    await session.commit()
    return removed


async def get_rules(session: AsyncSession, user: User) -> list[RuleRow]:
    return await list_rules(session, user.id, start_of_today())


async def _rule_row(session: AsyncSession, user: User, rule_id: int) -> RuleRow:
    for row in await list_rules(session, user.id, start_of_today()):
        if row.rule.id == rule_id:
            return row
    raise RuleNotFound("Правило не знайдено")


async def add_rule(session: AsyncSession, user: User, values: dict[str, Any]) -> RuleRow:
    """values — поля AlertRule без id і user_id (asset, fiat, exchange, payment, пороги, …)."""
    if await count_rules(session, user.id) >= MAX_RULES:
        raise LimitReached(f"Можна створити не більше {MAX_RULES} правил")
    rule = AlertRule(id=0, user_id=user.id, **values)
    if not has_threshold(rule):
        raise AlertError(
            "Задайте хоча б один поріг: спред, чистий спред, AI Score, RR або прибуток"
        )
    _check_channels(user, rule.channels)
    pair_id, exchange_id = await _resolve_pair(session, rule.asset, rule.fiat, rule.exchange)
    rule_id = await create_rule(
        session,
        {
            "user_id": user.id,
            "pair_id": pair_id,
            "exchange_id": exchange_id,
            "payment": normalize_payment(rule.payment) if rule.payment else None,
            "title": rule.title,
            **{field: getattr(rule, field) for field in THRESHOLD_FIELDS},
            "channels": list(dict.fromkeys(rule.channels)),
            "enabled": rule.enabled,
        },
    )
    await session.commit()
    return await _rule_row(session, user, rule_id)


async def change_rule(
    session: AsyncSession, user: User, rule_id: int, changes: dict[str, Any]
) -> RuleRow:
    """Часткова зміна. Пару, біржу й банк перевіряє так само, як при створенні."""
    current = await get_rule(session, user.id, rule_id)
    if current is None:
        raise RuleNotFound("Правило не знайдено")
    updated = current.model_copy(update=changes)
    if not has_threshold(updated):
        raise AlertError(
            "Задайте хоча б один поріг: спред, чистий спред, AI Score, RR або прибуток"
        )
    if "channels" in changes:
        _check_channels(user, updated.channels)
    values: dict[str, Any] = {
        field: getattr(updated, field)
        for field in (*THRESHOLD_FIELDS, "title", "enabled")
        if field in changes
    }
    if "channels" in changes:
        values["channels"] = list(dict.fromkeys(updated.channels))
    if "payment" in changes:
        values["payment"] = normalize_payment(updated.payment) if updated.payment else None
    if {"asset", "fiat", "exchange"} & changes.keys():
        pair_id, exchange_id = await _resolve_pair(
            session, updated.asset, updated.fiat, updated.exchange
        )
        values |= {"pair_id": pair_id, "exchange_id": exchange_id}
    await update_rule(session, user.id, rule_id, values)
    await session.commit()
    return await _rule_row(session, user, rule_id)


async def remove_rule(session: AsyncSession, user: User, rule_id: int) -> bool:
    removed = await delete_rule(session, user.id, rule_id)
    await session.commit()
    return removed


def _number(value: Decimal | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def alert_text(rule: AlertRule, item: Opportunity) -> str:
    """Текст сповіщення українською: однаковий для історії, Telegram і пошти."""
    title = rule.title or f"Правило #{rule.id}"
    route = (
        f"купити на {item.buy_exchange} за {_number(item.buy_price, 4)}, "
        f"продати на {item.sell_exchange} за {_number(item.sell_price, 4)}"
    )
    if item.kind == "chain":
        route = "ланцюжок: " + " → ".join(f"{step.exchange} {step.action}" for step in item.steps)
    lines = [
        f"🔔 {title}",
        f"{item.asset}/{item.fiat}: {route}",
        f"Спред {_number(item.spread_pct)} %, чистий {_number(item.net_spread_pct)} %, "
        f"прибуток ≈ {_number(item.estimated_profit)} {item.fiat}",
    ]
    extras = []
    if item.ai_score is not None:
        extras.append(f"AI Score {item.ai_score}")
    if item.rr is not None:
        extras.append(f"RR {_number(item.rr)}")
    if extras:
        lines.append(" · ".join(extras))
    lines.append("Аналітика, не фінансова порада. Перевірте умови на біржі перед угодою.")
    frontend = get_settings().frontend_url
    if frontend:
        lines.append(f"{frontend.rstrip('/')}/arbitrage")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class PendingDelivery:
    notification_id: int
    target: RuleTarget
    text: str
    delivery: dict[str, str]


class AlertDispatcher:
    """Звіряє нові можливості з правилами й доставляє сповіщення."""

    def __init__(
        self,
        telegram: TelegramClient | None = None,
        session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    ) -> None:
        self._telegram = telegram
        self._session_factory = session_factory
        self._tasks: set[asyncio.Task[None]] = set()

    async def dispatch(self, opened: list[tuple[Opportunity, int]]) -> int:
        """opened — нові можливості з id їхніх рядків. Повертає кількість сповіщень."""
        if not opened:
            return 0
        pairs = {(item.asset, item.fiat) for item, _ in opened}
        events: list[AlertEvent] = []
        pending: list[PendingDelivery] = []
        async with self._session_factory() as session, session.begin():
            for target in await rules_for_pairs(session, pairs):
                for item, opportunity_id in opened:
                    if not rule_matches(target.rule, item):
                        continue
                    text = alert_text(target.rule, item)
                    delivery = {"in_app": "sent"} if "in_app" in target.rule.channels else {}
                    notification_id, sent_at = await create_notification(
                        session,
                        {
                            "user_id": target.rule.user_id,
                            "alert_rule_id": target.rule.id,
                            "opportunity_id": opportunity_id,
                            "channels": target.rule.channels,
                            "delivery": delivery,
                            "text": text,
                            "data": item.model_dump(mode="json"),
                        },
                    )
                    events.append(
                        AlertEvent(
                            ts=datetime.now(UTC),
                            data=AlertNotification(
                                id=notification_id,
                                user_id=target.rule.user_id,
                                rule_id=target.rule.id,
                                rule_title=target.rule.title,
                                text=text,
                                channels=target.rule.channels,
                                opportunity=item,
                                created_at=sent_at,
                            ),
                        )
                    )
                    if {"telegram", "email"} & set(target.rule.channels):
                        pending.append(PendingDelivery(notification_id, target, text, delivery))
        for event in events:
            await publish_event(get_redis(), event)
        if pending:
            task = asyncio.create_task(self._deliver(pending))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        if events:
            log.info("alerts.dispatched", notifications=len(events), external=len(pending))
        return len(events)

    async def drain(self) -> None:
        """Чекає завершення фонової доставки (тести, зупинка collector)."""
        while self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _deliver(self, pending: list[PendingDelivery]) -> None:
        sent_per_user: dict[int, int] = defaultdict(int)
        for item in pending:
            user_id = item.target.rule.user_id
            delivery = dict(item.delivery)
            if sent_per_user[user_id] >= MAX_EXTERNAL_PER_USER:
                for channel in ("telegram", "email"):
                    if channel in item.target.rule.channels:
                        delivery[channel] = "skipped: limit"
            else:
                sent_per_user[user_id] += 1
                if "telegram" in item.target.rule.channels:
                    delivery["telegram"] = await self._send_telegram(item)
                if "email" in item.target.rule.channels:
                    delivery["email"] = await self._send_email(item)
            try:
                async with self._session_factory() as session, session.begin():
                    await set_delivery(session, item.notification_id, delivery)
            except Exception as exc:
                log.error("alerts.delivery_save_failed", error=repr(exc))

    async def _send_telegram(self, item: PendingDelivery) -> str:
        if self._telegram is None:
            return "skipped: bot disabled"
        if not item.target.telegram_chat_id:
            return "skipped: not linked"
        try:
            await self._telegram.send_message(item.target.telegram_chat_id, item.text)
        except TelegramError as exc:
            log.warning("alerts.telegram_failed", error=str(exc))
            return "failed"
        return "sent"

    async def _send_email(self, item: PendingDelivery) -> str:
        if not email_enabled():
            return "skipped: smtp disabled"
        subject = f"CryptoParser: {item.target.rule.title or 'сповіщення'}"
        try:
            await send_email(item.target.email, subject, item.text)
        except EmailError as exc:
            log.warning("alerts.email_failed", error=str(exc))
            return "failed"
        return "sent"
