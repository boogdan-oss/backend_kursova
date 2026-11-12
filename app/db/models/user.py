from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, str_enum
from app.db.models.enums import UserRole, UserStatus


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(str_enum(UserRole), default=UserRole.USER)
    status: Mapped[UserStatus] = mapped_column(str_enum(UserStatus), default=UserStatus.ACTIVE)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64))


class RefreshToken(TimestampMixin, Base):
    """Виданий refresh-токен. Відкликаний або прострочений вже не обмінюється на нову пару."""

    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
