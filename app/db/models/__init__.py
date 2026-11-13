"""Усі моделі — імпорт тут реєструє їх у Base.metadata (потрібно для Alembic)."""

from app.db.models.alert import AlertRule, Notification, WatchlistItem
from app.db.models.assistant import AssistantConversation, AssistantMessage
from app.db.models.community import Comment, Post, PostLike, Report, Repost, UserBan
from app.db.models.enums import (
    ContentStatus,
    ExchangeStatus,
    NotificationChannel,
    OpportunityKind,
    PredictionTarget,
    ReportStatus,
    RiskLevel,
    Side,
    UserRole,
    UserStatus,
)
from app.db.models.market_stats import MarketStats, SpreadHistory
from app.db.models.merchant import Merchant
from app.db.models.offer_snapshot import OfferSnapshot
from app.db.models.opportunity import Opportunity
from app.db.models.prediction import Prediction
from app.db.models.reference import (
    Asset,
    CurrencyPair,
    Exchange,
    ExchangeMarket,
    Fiat,
    PaymentMethod,
    PaymentMethodAlias,
)
from app.db.models.user import RefreshToken, User

__all__ = [
    "AlertRule",
    "Asset",
    "AssistantConversation",
    "AssistantMessage",
    "Comment",
    "ContentStatus",
    "CurrencyPair",
    "Exchange",
    "ExchangeMarket",
    "ExchangeStatus",
    "Fiat",
    "MarketStats",
    "Merchant",
    "Notification",
    "NotificationChannel",
    "OfferSnapshot",
    "Opportunity",
    "OpportunityKind",
    "PaymentMethod",
    "PaymentMethodAlias",
    "Post",
    "PostLike",
    "Prediction",
    "PredictionTarget",
    "RefreshToken",
    "Report",
    "ReportStatus",
    "Repost",
    "RiskLevel",
    "Side",
    "SpreadHistory",
    "User",
    "UserBan",
    "UserRole",
    "UserStatus",
    "WatchlistItem",
]
