"""Правила сповіщень (Smart Alerts, FR-16) і сповіщення, що з них виникають."""

from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel

from app.domain.opportunity import Opportunity

NotificationChannel = Literal["in_app", "telegram", "email"]


class AlertRule(BaseModel):
    """Пороги None — не перевіряються; exchange — купівля або продаж на цій біржі."""

    id: int
    user_id: int
    asset: str
    fiat: str
    exchange: str | None = None
    payment: str | None = None
    title: str | None = None
    min_spread: Decimal | None = None
    min_net_spread: Decimal | None = None
    min_ai_score: int | None = None
    min_rr: Decimal | None = None
    min_net_profit: Decimal | None = None
    channels: list[NotificationChannel]
    enabled: bool = True


class AlertNotification(BaseModel):
    """Спрацювання правила, як його бачить власник (WebSocket-подія alert, історія)."""

    id: int
    user_id: int
    rule_id: int | None
    rule_title: str | None
    text: str
    channels: list[NotificationChannel]
    opportunity: Opportunity | None
    created_at: AwareDatetime
