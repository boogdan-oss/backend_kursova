from datetime import datetime
from typing import Any, Literal

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RefreshToken, User, UserStatus

RefreshResult = Literal["ok", "reused", "invalid"]


async def get_user(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


async def find_user_by_login(session: AsyncSession, login: str) -> User | None:
    """Вхід за username або email, без урахування регістру."""
    value = login.strip().lower()
    query = select(User).where(or_(func.lower(User.username) == value, User.email == value))
    return (await session.execute(query)).scalars().first()


async def taken_fields(session: AsyncSession, username: str, email: str) -> set[str]:
    query = select(User.username, User.email).where(
        or_(func.lower(User.username) == username.lower(), User.email == email)
    )
    taken: set[str] = set()
    for existing_username, existing_email in (await session.execute(query)).all():
        if existing_username.lower() == username.lower():
            taken.add("username")
        if existing_email == email:
            taken.add("email")
    return taken


async def create_user(session: AsyncSession, username: str, email: str, password_hash: str) -> User:
    user = User(username=username, email=email, password_hash=password_hash, settings={})
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user


async def set_password(session: AsyncSession, user_id: int, password_hash: str) -> None:
    await session.execute(
        update(User).where(User.id == user_id).values(password_hash=password_hash)
    )


async def set_status(session: AsyncSession, user_id: int, status: UserStatus) -> None:
    await session.execute(update(User).where(User.id == user_id).values(status=status))


async def set_profile(session: AsyncSession, user_id: int, values: dict[str, Any]) -> None:
    await session.execute(update(User).where(User.id == user_id).values(**values))


async def set_settings(session: AsyncSession, user_id: int, settings: dict[str, Any]) -> None:
    await session.execute(update(User).where(User.id == user_id).values(settings=settings))


async def set_telegram_chat(session: AsyncSession, user_id: int, chat_id: str | None) -> None:
    await session.execute(update(User).where(User.id == user_id).values(telegram_chat_id=chat_id))


async def unlink_telegram_chat(session: AsyncSession, chat_id: str) -> int:
    result = await session.execute(
        update(User)
        .where(User.telegram_chat_id == chat_id)
        .values(telegram_chat_id=None)
        .returning(User.id)
    )
    return len(result.all())


async def save_refresh_token(
    session: AsyncSession, jti: str, user_id: int, expires_at: datetime
) -> None:
    await session.execute(
        delete(RefreshToken).where(
            RefreshToken.user_id == user_id, RefreshToken.expires_at < func.now()
        )
    )
    session.add(RefreshToken(id=jti, user_id=user_id, expires_at=expires_at))
    await session.flush()


async def consume_refresh_token(session: AsyncSession, jti: str, user_id: int) -> RefreshResult:
    """Відкликає токен і каже, чи він був дійсний. reused — його вже обміняли раніше."""
    result = await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.id == jti,
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > func.now(),
        )
        .values(revoked_at=func.now())
        .returning(RefreshToken.id)
    )
    if result.first() is not None:
        return "ok"
    existing = await session.get(RefreshToken, jti)
    if existing is not None and existing.user_id == user_id and existing.revoked_at is not None:
        return "reused"
    return "invalid"


async def revoke_refresh_token(session: AsyncSession, jti: str, user_id: int) -> None:
    await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.id == jti,
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=func.now())
    )


async def revoke_user_tokens(session: AsyncSession, user_id: int) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )


async def find_user_by_google_sub(session: AsyncSession, google_sub: str) -> User | None:
    query = select(User).where(User.google_sub == google_sub)
    return (await session.execute(query)).scalars().first()


async def find_user_by_email(session: AsyncSession, email: str) -> User | None:
    query = select(User).where(User.email == email.strip().lower())
    return (await session.execute(query)).scalars().first()


async def set_google_sub(session: AsyncSession, user_id: int, google_sub: str) -> None:
    await session.execute(update(User).where(User.id == user_id).values(google_sub=google_sub))


async def username_exists(session: AsyncSession, username: str) -> bool:
    query = select(User.id).where(func.lower(User.username) == username.lower())
    return (await session.execute(query)).first() is not None
