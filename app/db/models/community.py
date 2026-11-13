"""Модуль Crypto Community (FR-31 – FR-46)."""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, str_enum
from app.db.models.enums import ContentStatus, ReportStatus


class Post(TimestampMixin, Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    attached_pair_id: Mapped[int | None] = mapped_column(
        ForeignKey("currency_pairs.id", ondelete="SET NULL")
    )
    attached_opportunity_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[ContentStatus] = mapped_column(
        str_enum(ContentStatus), default=ContentStatus.VISIBLE
    )
    updated_at: Mapped[datetime | None]
    moderated_at: Mapped[datetime | None]
    moderated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


Index("ix_posts_status_created_at", Post.status, Post.created_at.desc())


class PostLike(TimestampMixin, Base):
    __tablename__ = "post_likes"

    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )


class Comment(TimestampMixin, Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[ContentStatus] = mapped_column(
        str_enum(ContentStatus), default=ContentStatus.VISIBLE
    )
    updated_at: Mapped[datetime | None]
    moderated_at: Mapped[datetime | None]
    moderated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class Repost(TimestampMixin, Base):
    __tablename__ = "reposts"

    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )


class Report(TimestampMixin, Base):
    """Скарга на публікацію або коментар (FR-43) — рівно одна ціль."""

    __tablename__ = "reports"
    __table_args__ = (
        CheckConstraint("(post_id IS NULL) <> (comment_id IS NULL)", name="single_target"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int | None] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), index=True
    )
    comment_id: Mapped[int | None] = mapped_column(
        ForeignKey("comments.id", ondelete="CASCADE"), index=True
    )
    reporter_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    reason: Mapped[str] = mapped_column(String(500))
    details: Mapped[str | None] = mapped_column(String(1000))
    status: Mapped[ReportStatus] = mapped_column(
        str_enum(ReportStatus), default=ReportStatus.PENDING, index=True
    )
    resolved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    resolved_at: Mapped[datetime | None]


class UserBan(TimestampMixin, Base):
    """Блокування користувача (FR-45). until NULL — назавжди."""

    __tablename__ = "user_bans"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    until: Mapped[datetime | None]
    reason: Mapped[str] = mapped_column(String(500))
    banned_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    lifted_at: Mapped[datetime | None]


Index("ix_reposts_user_id_created_at", Repost.user_id, Repost.created_at)
Index("ix_post_likes_user_id_created_at", PostLike.user_id, PostLike.created_at)
