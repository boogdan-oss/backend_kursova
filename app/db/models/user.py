from typing import Any

from sqlalchemy import String
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
    # Персональні налаштування (/me/settings): комісії, мін. RR, сума інвестиції тощо
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    # Для каналу сповіщень Telegram; NULL — користувач не підключив бота
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64))
