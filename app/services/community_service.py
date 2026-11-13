"""Спільнота (FR-31 – FR-46): стрічка, публікації, коментарі, профілі, скарги й модерація.

Права перевіряються тут: редагувати й видаляти — лише автор, модерувати — лише адмін.
Прихований чи видалений контент для звичайних користувачів не існує (404).
Кожна функція, що змінює дані, сама комітить сесію.
"""

import base64
import binascii
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Comment, ContentStatus, Post, ReportStatus, User, UserRole, UserStatus
from app.db.repositories import community as repo
from app.db.repositories.community import FeedRow, TimelineScope
from app.db.repositories.users import get_user, revoke_user_tokens, set_status
from app.domain import (
    BanView,
    CommentView,
    FeedItem,
    ModerationStats,
    PairRef,
    PostView,
    ProfileView,
    ReportReason,
    ReportView,
    TrendingPair,
)

FeedSort = Literal["new", "popular"]
FeedScope = Literal["all", "mine"]
ProfileTab = Literal["posts", "reposts", "likes"]
ReportAction = Literal["hide", "delete", "dismiss"]

POPULAR_PERIOD = timedelta(days=7)

log = structlog.get_logger()


class CommunityError(Exception):
    pass


class NotFound(CommunityError):
    pass


class Forbidden(CommunityError):
    pass


class Conflict(CommunityError):
    pass


class BadRequest(CommunityError):
    pass


@dataclass(frozen=True, slots=True)
class FeedPage:
    items: list[FeedItem]
    next_cursor: str | None


def _is_admin(user: User | None) -> bool:
    return user is not None and user.role == UserRole.ADMIN


def _encode(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode(cursor: str) -> str:
    try:
        return base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise BadRequest("Невірний курсор сторінки") from None


def timeline_cursor(row: FeedRow) -> str:
    return _encode(f"t|{row.at.isoformat()}|{row.post_id}|{row.reposter_id or 0}")


def parse_timeline_cursor(cursor: str | None) -> FeedRow | None:
    if cursor is None:
        return None
    parts = _decode(cursor).split("|")
    try:
        if len(parts) != 4 or parts[0] != "t":
            raise ValueError
        at = datetime.fromisoformat(parts[1])
        if at.tzinfo is None:
            raise ValueError
        return FeedRow(int(parts[2]), int(parts[3]) or None, at)
    except ValueError:
        raise BadRequest("Невірний курсор сторінки") from None


def parse_offset_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    kind, _, value = _decode(cursor).partition("|")
    if kind != "o" or not value.isdigit():
        raise BadRequest("Невірний курсор сторінки")
    return int(value)


async def _items(session: AsyncSession, rows: list[FeedRow], viewer: User | None) -> list[FeedItem]:
    views = await repo.post_views(session, [row.post_id for row in rows], _viewer_id(viewer))
    reposters = await repo.authors(
        session, [row.reposter_id for row in rows if row.reposter_id is not None]
    )
    return [
        FeedItem(
            post=views[row.post_id],
            reposted_by=None if row.reposter_id is None else reposters.get(row.reposter_id),
            at=row.at,
        )
        for row in rows
        if row.post_id in views
    ]


def _viewer_id(viewer: User | None) -> int | None:
    return None if viewer is None else viewer.id


async def _pair_id(session: AsyncSession, pair: PairRef) -> int:
    pair_id = await repo.find_pair_id(session, pair.asset, pair.fiat)
    if pair_id is None:
        raise NotFound(f"Пари {pair.asset}/{pair.fiat} немає в довіднику бірж")
    return pair_id


async def _timeline_page(
    session: AsyncSession,
    scope: TimelineScope,
    viewer: User | None,
    cursor: str | None,
    limit: int,
) -> FeedPage:
    rows = await repo.timeline(session, scope, parse_timeline_cursor(cursor), limit)
    next_cursor = timeline_cursor(rows[-1]) if len(rows) == limit else None
    return FeedPage(await _items(session, rows, viewer), next_cursor)


async def feed(
    session: AsyncSession,
    viewer: User | None,
    sort: FeedSort,
    scope: FeedScope,
    pair: PairRef | None,
    cursor: str | None,
    limit: int,
) -> FeedPage:
    """Стрічка (FR-31): нові — публікації й репости за часом; популярні — за 7 днів за балами."""
    if scope == "mine" and viewer is None:
        raise Forbidden("Щоб побачити свої публікації, увійдіть")
    pair_id = None if pair is None else await _pair_id(session, pair)
    user_id = viewer.id if scope == "mine" and viewer is not None else None
    if sort == "new":
        scope_filter = TimelineScope(user_id=user_id, pair_id=pair_id)
        return await _timeline_page(session, scope_filter, viewer, cursor, limit)
    offset = parse_offset_cursor(cursor)
    since = datetime.now(UTC) - POPULAR_PERIOD
    ids = await repo.popular_post_ids(session, since, pair_id, user_id, offset, limit)
    views = await repo.post_views(session, ids, _viewer_id(viewer))
    items = [
        FeedItem(post=views[post_id], reposted_by=None, at=views[post_id].created_at)
        for post_id in ids
        if post_id in views
    ]
    next_cursor = _encode(f"o|{offset + limit}") if len(ids) == limit else None
    return FeedPage(items, next_cursor)


async def _profile_user(session: AsyncSession, username: str) -> User:
    user = await repo.user_by_username(session, username)
    if user is None:
        raise NotFound("Користувача не знайдено")
    return user


async def user_feed(
    session: AsyncSession,
    viewer: User | None,
    username: str,
    tab: ProfileTab,
    cursor: str | None,
    limit: int,
) -> FeedPage:
    """Публікації, репости або вподобане користувача (FR-40)."""
    user = await _profile_user(session, username)
    if tab == "likes":
        rows = await repo.liked_rows(session, user.id, parse_timeline_cursor(cursor), limit)
        next_cursor = timeline_cursor(rows[-1]) if len(rows) == limit else None
        return FeedPage(await _items(session, rows, viewer), next_cursor)
    scope = TimelineScope(posts=tab == "posts", reposts=tab == "reposts", user_id=user.id)
    return await _timeline_page(session, scope, viewer, cursor, limit)


async def profile(session: AsyncSession, viewer: User | None, username: str) -> ProfileView:
    """Публічний профіль; адміну — ще чинне блокування."""
    user = await _profile_user(session, username)
    view = await repo.profile_view(session, user)
    if _is_admin(viewer):
        ban = await repo.active_ban(session, user.id, datetime.now(UTC))
        if ban is not None:
            view = view.model_copy(update={"ban_until": ban.until, "ban_reason": ban.reason})
    return view


async def _visible_post(session: AsyncSession, post_id: int, viewer: User | None) -> Post:
    post = await repo.get_post(session, post_id)
    if post is None or (post.status != ContentStatus.VISIBLE and not _is_admin(viewer)):
        raise NotFound("Публікацію не знайдено")
    return post


async def _post_view(session: AsyncSession, post_id: int, viewer: User | None) -> PostView:
    views = await repo.post_views(session, [post_id], _viewer_id(viewer))
    if post_id not in views:
        raise NotFound("Публікацію не знайдено")
    return views[post_id]


async def get_post(session: AsyncSession, post_id: int, viewer: User | None) -> PostView:
    await _visible_post(session, post_id, viewer)
    return await _post_view(session, post_id, viewer)


async def _attachments(session: AsyncSession, changes: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if "text" in changes:
        values["text"] = changes["text"]
    if "pair" in changes:
        pair = changes["pair"]
        values["attached_pair_id"] = None if pair is None else await _pair_id(session, pair)
    if "opportunity_id" in changes:
        opportunity_id = changes["opportunity_id"]
        if opportunity_id is not None and not await repo.opportunity_exists(
            session, opportunity_id
        ):
            raise NotFound("Можливість не знайдено в історії")
        values["attached_opportunity_id"] = opportunity_id
    return values


async def create_post(
    session: AsyncSession,
    author: User,
    text: str,
    pair: PairRef | None,
    opportunity_id: int | None,
) -> PostView:
    """Нова публікація (FR-32) з парою чи можливістю (FR-42)."""
    values = await _attachments(
        session, {"text": text, "pair": pair, "opportunity_id": opportunity_id}
    )
    post_id = await repo.add_post(session, {"author_id": author.id, **values})
    await session.commit()
    log.info("community.post_created", post_id=post_id, user_id=author.id)
    return await _post_view(session, post_id, author)


async def _own_post(session: AsyncSession, post_id: int, user: User) -> Post:
    post = await repo.get_post(session, post_id)
    if post is None or post.status != ContentStatus.VISIBLE:
        raise NotFound("Публікацію не знайдено")
    if post.author_id != user.id:
        raise Forbidden("Змінювати й видаляти можна лише власні публікації")
    return post


async def edit_post(
    session: AsyncSession, user: User, post_id: int, changes: dict[str, Any]
) -> PostView:
    """Редагування власної публікації (FR-33). У changes — лише поля, які треба змінити."""
    await _own_post(session, post_id, user)
    values = await _attachments(session, changes)
    if values:
        await repo.set_post(session, post_id, {**values, "updated_at": datetime.now(UTC)})
        await session.commit()
    return await _post_view(session, post_id, user)


async def delete_post(session: AsyncSession, user: User, post_id: int) -> None:
    """Видалення власної публікації (FR-34): запис лишається зі статусом deleted."""
    await _own_post(session, post_id, user)
    await repo.set_post(session, post_id, {"status": ContentStatus.DELETED})
    await session.commit()


async def set_like(session: AsyncSession, user: User, post_id: int, liked: bool) -> PostView:
    """Лайк і його скасування (FR-35); повтор нічого не змінює."""
    await _visible_post(session, post_id, None)
    if liked:
        await repo.like(session, post_id, user.id)
    else:
        await repo.unlike(session, post_id, user.id)
    await session.commit()
    return await _post_view(session, post_id, user)


async def set_repost(session: AsyncSession, user: User, post_id: int, reposted: bool) -> PostView:
    """Репост і його скасування (FR-38, FR-39). Власну публікацію репостити не можна."""
    post = await _visible_post(session, post_id, None)
    if reposted:
        if post.author_id == user.id:
            raise BadRequest("Власну публікацію репостити не можна")
        await repo.repost(session, post_id, user.id)
    else:
        await repo.unrepost(session, post_id, user.id)
    await session.commit()
    return await _post_view(session, post_id, user)


async def comments(
    session: AsyncSession, post_id: int, viewer: User | None, limit: int
) -> list[CommentView]:
    await _visible_post(session, post_id, viewer)
    return await repo.comment_views(session, post_id, limit)


async def _comment_view(session: AsyncSession, comment_id: int) -> CommentView:
    view = await repo.comment_view(session, comment_id)
    if view is None:
        raise NotFound("Коментар не знайдено")
    return view


async def add_comment(session: AsyncSession, user: User, post_id: int, text: str) -> CommentView:
    """Коментар до видимої публікації (FR-36)."""
    await _visible_post(session, post_id, None)
    comment_id = await repo.add_comment(session, post_id, user.id, text)
    await session.commit()
    return await _comment_view(session, comment_id)


async def _own_comment(session: AsyncSession, comment_id: int, user: User) -> Comment:
    comment = await repo.get_comment(session, comment_id)
    if comment is None or comment.status != ContentStatus.VISIBLE:
        raise NotFound("Коментар не знайдено")
    post = await repo.get_post(session, comment.post_id)
    if post is None or post.status != ContentStatus.VISIBLE:
        raise NotFound("Коментар не знайдено")
    if comment.author_id != user.id:
        raise Forbidden("Змінювати й видаляти можна лише власні коментарі")
    return comment


async def edit_comment(
    session: AsyncSession, user: User, comment_id: int, text: str
) -> CommentView:
    """Редагування власного коментаря (FR-37)."""
    await _own_comment(session, comment_id, user)
    await repo.set_comment(session, comment_id, {"text": text, "updated_at": datetime.now(UTC)})
    await session.commit()
    return await _comment_view(session, comment_id)


async def delete_comment(session: AsyncSession, user: User, comment_id: int) -> None:
    """Видалення власного коментаря (FR-37)."""
    await _own_comment(session, comment_id, user)
    await repo.set_comment(session, comment_id, {"status": ContentStatus.DELETED})
    await session.commit()


async def _report_view(session: AsyncSession, report_id: int) -> ReportView:
    for view in await repo.report_views(session, None, 1, report_id):
        return view
    raise NotFound("Скаргу не знайдено")


async def report(
    session: AsyncSession,
    reporter: User,
    post_id: int | None,
    comment_id: int | None,
    reason: ReportReason,
    details: str | None,
) -> ReportView:
    """Скарга на публікацію або коментар (FR-43). Одна відкрита скарга на ціль від людини."""
    if (post_id is None) == (comment_id is None):
        raise BadRequest("Вкажіть або post_id, або comment_id")
    if post_id is not None:
        author_id = (await _visible_post(session, post_id, None)).author_id
    else:
        assert comment_id is not None
        comment = await repo.get_comment(session, comment_id)
        post = None if comment is None else await repo.get_post(session, comment.post_id)
        visible = (
            comment is not None
            and post is not None
            and comment.status == post.status == ContentStatus.VISIBLE
        )
        if comment is None or not visible:
            raise NotFound("Коментар не знайдено")
        author_id = comment.author_id
    if author_id == reporter.id:
        raise BadRequest("Скаржитись на власний контент не можна")
    if await repo.pending_report_exists(session, reporter.id, post_id, comment_id):
        raise Conflict("Ви вже поскаржились, скарга на розгляді")
    report_id = await repo.add_report(
        session,
        {
            "post_id": post_id,
            "comment_id": comment_id,
            "reporter_id": reporter.id,
            "reason": reason,
            "details": details,
        },
    )
    await session.commit()
    log.info("community.reported", report_id=report_id, user_id=reporter.id)
    return await _report_view(session, report_id)


async def trending(session: AsyncSession, hours: int, limit: int) -> list[TrendingPair]:
    """Найобговорюваніші пари за період і зміна їхньої ціни купівлі за той самий час."""
    now = datetime.now(UTC)
    since = now - timedelta(hours=hours)
    rows = await repo.trending_pairs(session, since, limit)
    changes = await repo.buy_price_change(session, [row.pair_id for row in rows], since, now)
    return [
        TrendingPair(
            asset=row.asset,
            fiat=row.fiat,
            posts=row.posts,
            likes=row.likes,
            comments=row.comments,
            change_pct=changes.get(row.pair_id),
        )
        for row in rows
    ]


async def moderation_stats(session: AsyncSession) -> ModerationStats:
    return await repo.moderation_stats(session, datetime.now(UTC))


async def reports(
    session: AsyncSession, status: ReportStatus | None, limit: int
) -> list[ReportView]:
    return await repo.report_views(session, status, limit)


def _verdict(status: ContentStatus) -> ReportStatus:
    """Прихований чи видалений контент — скарги підтверджено, відновлений — відхилено."""
    return ReportStatus.REJECTED if status == ContentStatus.VISIBLE else ReportStatus.RESOLVED


def _moderation(status: ContentStatus, admin: User) -> dict[str, Any]:
    return {"status": status, "moderated_at": datetime.now(UTC), "moderated_by_id": admin.id}


async def set_post_status(
    session: AsyncSession, admin: User, post_id: int, status: ContentStatus
) -> PostView:
    """Приховати, видалити чи відновити публікацію (FR-44, FR-46); відкриті скарги закриваються."""
    if await repo.get_post(session, post_id) is None:
        raise NotFound("Публікацію не знайдено")
    await repo.set_post(session, post_id, _moderation(status, admin))
    await repo.resolve_reports(session, post_id, None, _verdict(status), admin.id)
    await session.commit()
    log.info("community.post_moderated", post_id=post_id, status=status, admin_id=admin.id)
    return await _post_view(session, post_id, admin)


async def set_comment_status(
    session: AsyncSession, admin: User, comment_id: int, status: ContentStatus
) -> CommentView:
    """Приховати, видалити чи відновити коментар (FR-44, FR-46)."""
    if await repo.get_comment(session, comment_id) is None:
        raise NotFound("Коментар не знайдено")
    await repo.set_comment(session, comment_id, _moderation(status, admin))
    await repo.resolve_reports(session, None, comment_id, _verdict(status), admin.id)
    await session.commit()
    log.info("community.comment_moderated", comment_id=comment_id, status=status)
    return await _comment_view(session, comment_id)


async def resolve_report(
    session: AsyncSession, admin: User, report_id: int, action: ReportAction
) -> ReportView:
    """Рішення за скаргою: приховати чи видалити ціль або відхилити скаргу.

    Рішення стосується цілі, тож закриває всі відкриті скарги на неї.
    """
    found = await repo.get_report(session, report_id)
    if found is None:
        raise NotFound("Скаргу не знайдено")
    if found.status != ReportStatus.PENDING:
        raise Conflict("Скаргу вже розглянуто")
    if action == "dismiss":
        await repo.resolve_reports(
            session, found.post_id, found.comment_id, ReportStatus.REJECTED, admin.id
        )
        await session.commit()
    else:
        status = ContentStatus.HIDDEN if action == "hide" else ContentStatus.DELETED
        if found.post_id is not None:
            await set_post_status(session, admin, found.post_id, status)
        elif found.comment_id is not None:
            await set_comment_status(session, admin, found.comment_id, status)
    return await _report_view(session, report_id)


async def hidden(
    session: AsyncSession, admin: User, limit: int
) -> tuple[list[PostView], list[CommentView]]:
    """Приховане модерацією — щоб його можна було відновити (FR-46)."""
    ids = await repo.hidden_post_ids(session, limit)
    views = await repo.post_views(session, ids, admin.id)
    posts = [views[post_id] for post_id in ids if post_id in views]
    return posts, await repo.hidden_comment_views(session, limit)


async def bans(session: AsyncSession, active_only: bool) -> list[BanView]:
    return await repo.ban_views(session, active_only, datetime.now(UTC))


async def ban(
    session: AsyncSession, admin: User, user_id: int, days: int | None, reason: str
) -> BanView:
    """Тимчасове чи безстрокове блокування (FR-45). Нове замінює чинне; сесії відкликаються."""
    user = await get_user(session, user_id)
    if user is None:
        raise NotFound("Користувача не знайдено")
    if user.id == admin.id or user.role == UserRole.ADMIN:
        raise BadRequest("Адміністратора заблокувати не можна")
    now = datetime.now(UTC)
    await repo.lift_bans(session, user.id)
    ban_id = await repo.add_ban(
        session,
        {
            "user_id": user.id,
            "until": None if days is None else now + timedelta(days=days),
            "reason": reason,
            "banned_by_id": admin.id,
        },
    )
    await set_status(session, user.id, UserStatus.BANNED if days is None else UserStatus.SUSPENDED)
    await revoke_user_tokens(session, user.id)
    await session.commit()
    log.info("community.user_banned", user_id=user.id, days=days, admin_id=admin.id)
    for view in await repo.ban_views(session, False, now, ban_id):
        return view
    raise NotFound("Блокування не знайдено")


async def unban(session: AsyncSession, admin: User, user_id: int) -> None:
    """Зняти всі чинні блокування користувача."""
    user = await get_user(session, user_id)
    if user is None:
        raise NotFound("Користувача не знайдено")
    await repo.lift_bans(session, user.id)
    await set_status(session, user.id, UserStatus.ACTIVE)
    await session.commit()
    log.info("community.user_unbanned", user_id=user.id, admin_id=admin.id)
