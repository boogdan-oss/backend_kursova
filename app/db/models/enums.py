from enum import StrEnum


class UserRole(StrEnum):
    USER = "user"
    ADMIN = "admin"


class UserStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    BANNED = "banned"


class ExchangeStatus(StrEnum):
    ACTIVE = "active"
    DEGRADED = "degraded"
    DISABLED = "disabled"


class Side(StrEnum):
    """Сторона з погляду користувача."""

    BUY = "BUY"
    SELL = "SELL"


class OpportunityKind(StrEnum):
    SPREAD = "spread"
    CROSS_EXCHANGE = "cross_exchange"
    CHAIN = "chain"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PredictionTarget(StrEnum):
    BUY_PRICE = "buy_price"
    SELL_PRICE = "sell_price"
    SPREAD = "spread"


class NotificationChannel(StrEnum):
    IN_APP = "in_app"
    TELEGRAM = "telegram"
    EMAIL = "email"


class ContentStatus(StrEnum):
    VISIBLE = "visible"
    HIDDEN = "hidden"
    DELETED = "deleted"


class ReportStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    REJECTED = "rejected"
