"""Спільнота (FR-31 – FR-46): публікації, коментарі, профілі, скарги, блокування."""

from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel

ContentState = Literal["visible", "hidden", "deleted"]
ReportState = Literal["pending", "resolved", "rejected"]
ReportReason = Literal["spam", "scam", "abuse", "misinformation", "other"]


class Author(BaseModel):
    id: int
    username: str
    is_admin: bool = False


class PairRef(BaseModel):
    asset: str
    fiat: str


class AttachedOpportunity(BaseModel):
    """Знімок прикріпленої можливості з історії (FR-42)."""

    id: int
    kind: str
    asset: str
    fiat: str
    buy_exchange: str
    sell_exchange: str
    buy_price: Decimal
    sell_price: Decimal
    spread_pct: Decimal
    net_spread_pct: Decimal
    estimated_profit: Decimal
    ai_score: int | None
    detected_at: AwareDatetime
    duration_sec: int | None


class PostView(BaseModel):
    """Публікація з лічильниками (FR-41) і тим, що з нею зробив поточний користувач."""

    id: int
    author: Author
    text: str
    pair: PairRef | None
    opportunity: AttachedOpportunity | None
    status: ContentState
    likes: int
    comments: int
    reposts: int
    liked_by_me: bool
    reposted_by_me: bool
    created_at: AwareDatetime
    updated_at: AwareDatetime | None


class FeedItem(BaseModel):
    """Елемент стрічки: публікація або її репост (reposted_by — хто поділився)."""

    post: PostView
    reposted_by: Author | None
    at: AwareDatetime


class CommentView(BaseModel):
    id: int
    post_id: int
    author: Author
    text: str
    status: ContentState
    created_at: AwareDatetime
    updated_at: AwareDatetime | None


class ProfileView(BaseModel):
    """Публічний профіль (FR-40): без email і налаштувань."""

    id: int
    username: str
    display_name: str | None = None
    bio: str | None = None
    is_admin: bool
    status: str
    joined_at: AwareDatetime
    posts: int
    likes_received: int
    reposts_received: int
    comments: int = 0
    ban_until: AwareDatetime | None = None
    ban_reason: str | None = None


class ReportView(BaseModel):
    id: int
    target: Literal["post", "comment"]
    post_id: int
    comment_id: int | None
    target_text: str
    target_status: ContentState
    target_author: Author
    reporter: Author
    reason: ReportReason
    details: str | None
    status: ReportState
    created_at: AwareDatetime
    resolved_at: AwareDatetime | None


class BanView(BaseModel):
    id: int
    user: Author
    reason: str
    until: AwareDatetime | None
    banned_by: Author | None
    created_at: AwareDatetime
    lifted_at: AwareDatetime | None


class TrendingPair(BaseModel):
    """Пара, яку найбільше обговорюють у видимих публікаціях за період."""

    asset: str
    fiat: str
    posts: int
    likes: int
    comments: int
    change_pct: Decimal | None = None


class ModerationStats(BaseModel):
    """Лічильники для панелі модерації (FR-44 – FR-46)."""

    reports_new: int
    reports_pending: int
    hidden: int
    deleted_7d: int
    banned: int
