"""Запити модуля спільноти. Видимість і права перевіряє сервіс."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, get_args

from sqlalchemy import (
    Integer,
    Select,
    and_,
    cast,
    delete,
    exists,
    false,
    func,
    null,
    or_,
    select,
    tuple_,
    union_all,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models import (
    Asset,
    Comment,
    ContentStatus,
    CurrencyPair,
    Exchange,
    Fiat,
    MarketStats,
    Opportunity,
    Post,
    PostLike,
    Report,
    ReportStatus,
    Repost,
    User,
    UserBan,
    UserRole,
)
from app.domain.community import (
    AttachedOpportunity,
    Author,
    BanView,
    CommentView,
    ModerationStats,
    PairRef,
    PostView,
    ProfileView,
    ReportReason,
    ReportView,
)

REPORT_REASONS: frozenset[str] = frozenset(get_args(ReportReason))
PRICE_WINDOW = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class FeedRow:
    post_id: int
    reposter_id: int | None
    at: datetime


def _author(user_id: int, username: str, role: str) -> Author:
    return Author(id=user_id, username=username, is_admin=role == UserRole.ADMIN)


async def authors(session: AsyncSession, ids: Sequence[int]) -> dict[int, Author]:
    if not ids:
        return {}
    rows = await session.execute(
        select(User.id, User.username, User.role).where(User.id.in_(set(ids)))
    )
    return {row.id: _author(row.id, row.username, row.role) for row in rows}


async def find_pair_id(session: AsyncSession, asset: str, fiat: str) -> int | None:
    query = (
        select(CurrencyPair.id)
        .join(Asset, Asset.id == CurrencyPair.base_currency_id)
        .join(Fiat, Fiat.id == CurrencyPair.quote_currency_id)
        .where(Asset.code == asset, Fiat.code == fiat)
    )
    return (await session.execute(query)).scalar_one_or_none()


async def opportunity_exists(session: AsyncSession, opportunity_id: int) -> bool:
    query = select(Opportunity.id).where(Opportunity.id == opportunity_id).limit(1)
    return (await session.execute(query)).first() is not None


async def attached_opportunities(
    session: AsyncSession, ids: Sequence[int]
) -> dict[int, AttachedOpportunity]:
    if not ids:
        return {}
    buy, sell = aliased(Exchange), aliased(Exchange)
    query = (
        select(Opportunity, Asset.code, Fiat.code, buy.code, sell.code)
        .join(CurrencyPair, CurrencyPair.id == Opportunity.pair_id)
        .join(Asset, Asset.id == CurrencyPair.base_currency_id)
        .join(Fiat, Fiat.id == CurrencyPair.quote_currency_id)
        .join(buy, buy.id == Opportunity.buy_exchange_id)
        .join(sell, sell.id == Opportunity.sell_exchange_id)
        .where(Opportunity.id.in_(set(ids)))
    )
    found: dict[int, AttachedOpportunity] = {}
    for row, asset, fiat, buy_code, sell_code in (await session.execute(query)).all():
        found[row.id] = AttachedOpportunity(
            id=row.id,
            kind=row.kind,
            asset=asset,
            fiat=fiat,
            buy_exchange=buy_code,
            sell_exchange=sell_code,
            buy_price=row.buy_price,
            sell_price=row.sell_price,
            spread_pct=row.spread,
            net_spread_pct=row.net_spread,
            estimated_profit=row.estimated_profit,
            ai_score=row.ai_score,
            detected_at=row.time,
            duration_sec=row.duration_sec,
        )
    return found


def _counters(viewer_id: int | None) -> list[Any]:
    likes = (
        select(func.count())
        .where(PostLike.post_id == Post.id)
        .correlate(Post)
        .scalar_subquery()
        .label("likes")
    )
    comments = (
        select(func.count())
        .where(Comment.post_id == Post.id, Comment.status == ContentStatus.VISIBLE)
        .correlate(Post)
        .scalar_subquery()
        .label("comments")
    )
    reposts = (
        select(func.count())
        .where(Repost.post_id == Post.id)
        .correlate(Post)
        .scalar_subquery()
        .label("reposts")
    )
    if viewer_id is None:
        liked, reposted = false().label("liked"), false().label("reposted")
    else:
        liked = (
            exists()
            .where(PostLike.post_id == Post.id, PostLike.user_id == viewer_id)
            .correlate(Post)
            .label("liked")
        )
        reposted = (
            exists()
            .where(Repost.post_id == Post.id, Repost.user_id == viewer_id)
            .correlate(Post)
            .label("reposted")
        )
    return [likes, comments, reposts, liked, reposted]


async def post_views(
    session: AsyncSession, ids: Sequence[int], viewer_id: int | None
) -> dict[int, PostView]:
    """Публікації з автором, парою, можливістю й лічильниками; ключ — id."""
    if not ids:
        return {}
    query = (
        select(Post, User.username, User.role, Asset.code, Fiat.code, *_counters(viewer_id))
        .join(User, User.id == Post.author_id)
        .outerjoin(CurrencyPair, CurrencyPair.id == Post.attached_pair_id)
        .outerjoin(Asset, Asset.id == CurrencyPair.base_currency_id)
        .outerjoin(Fiat, Fiat.id == CurrencyPair.quote_currency_id)
        .where(Post.id.in_(set(ids)))
    )
    rows = (await session.execute(query)).all()
    opportunity_ids = [row[0].attached_opportunity_id for row in rows]
    opportunities = await attached_opportunities(
        session, [item for item in opportunity_ids if item is not None]
    )
    views: dict[int, PostView] = {}
    for post, username, role, asset, fiat, likes, comments, reposts, liked, reposted in rows:
        views[post.id] = PostView(
            id=post.id,
            author=_author(post.author_id, username, role),
            text=post.text,
            pair=PairRef(asset=asset, fiat=fiat) if asset and fiat else None,
            opportunity=(
                opportunities.get(post.attached_opportunity_id)
                if post.attached_opportunity_id is not None
                else None
            ),
            status=post.status,
            likes=likes,
            comments=comments,
            reposts=reposts,
            liked_by_me=bool(liked),
            reposted_by_me=bool(reposted),
            created_at=post.created_at,
            updated_at=post.updated_at,
        )
    return views


@dataclass(frozen=True, slots=True)
class TimelineScope:
    """Що потрапляє в стрічку.

    user_id — лише публікації цього автора та його репости (стрічка «Мої» і профіль).
    """

    posts: bool = True
    reposts: bool = True
    user_id: int | None = None
    pair_id: int | None = None


def _visible_with_pair(pair_id: int | None) -> list[Any]:
    conditions: list[Any] = [Post.status == ContentStatus.VISIBLE]
    if pair_id is not None:
        conditions.append(Post.attached_pair_id == pair_id)
    return conditions


async def timeline(
    session: AsyncSession, scope: TimelineScope, after: FeedRow | None, limit: int
) -> list[FeedRow]:
    """Видимі публікації та репости, новіші першими (FR-31). after — останній рядок сторінки."""
    parts: list[Any] = []
    if scope.posts:
        posts = select(
            Post.id.label("post_id"),
            cast(null(), Integer).label("reposter_id"),
            Post.created_at.label("at"),
        ).where(*_visible_with_pair(scope.pair_id))
        if scope.user_id is not None:
            posts = posts.where(Post.author_id == scope.user_id)
        parts.append(posts)
    if scope.reposts:
        reposts = (
            select(
                Repost.post_id.label("post_id"),
                Repost.user_id.label("reposter_id"),
                Repost.created_at.label("at"),
            )
            .join(Post, Post.id == Repost.post_id)
            .where(*_visible_with_pair(scope.pair_id))
        )
        if scope.user_id is not None:
            reposts = reposts.where(Repost.user_id == scope.user_id)
        parts.append(reposts)
    if not parts:
        return []
    combined: Any = parts[0] if len(parts) == 1 else union_all(parts[0], parts[1])
    merged = combined.subquery()
    rank = func.coalesce(merged.c.reposter_id, 0)
    query = select(merged.c.post_id, merged.c.reposter_id, merged.c.at)
    if after is not None:
        query = query.where(
            tuple_(merged.c.at, merged.c.post_id, rank)
            < tuple_(after.at, after.post_id, after.reposter_id or 0)
        )
    query = query.order_by(merged.c.at.desc(), merged.c.post_id.desc(), rank.desc()).limit(limit)
    return [FeedRow(*row) for row in (await session.execute(query)).all()]


async def liked_rows(
    session: AsyncSession, user_id: int, after: FeedRow | None, limit: int
) -> list[FeedRow]:
    """Видимі публікації, які вподобав користувач; at — час лайку."""
    query = (
        select(PostLike.post_id, PostLike.created_at)
        .join(Post, Post.id == PostLike.post_id)
        .where(PostLike.user_id == user_id, Post.status == ContentStatus.VISIBLE)
    )
    if after is not None:
        query = query.where(
            tuple_(PostLike.created_at, PostLike.post_id) < tuple_(after.at, after.post_id)
        )
    query = query.order_by(PostLike.created_at.desc(), PostLike.post_id.desc()).limit(limit)
    return [FeedRow(post_id, None, at) for post_id, at in (await session.execute(query)).all()]


async def popular_post_ids(
    session: AsyncSession,
    since: datetime,
    pair_id: int | None,
    author_id: int | None,
    offset: int,
    limit: int,
) -> list[int]:
    """Найобговорюваніші за період: лайк — 1, коментар — 2, репост — 3 бали."""
    likes, comments, reposts, *_ = _counters(None)
    score = likes + comments * 2 + reposts * 3
    query = (
        select(Post.id)
        .where(*_visible_with_pair(pair_id), Post.created_at >= since)
        .order_by(score.desc(), Post.created_at.desc(), Post.id.desc())
        .offset(offset)
        .limit(limit)
    )
    if author_id is not None:
        query = query.where(Post.author_id == author_id)
    return list((await session.execute(query)).scalars())


@dataclass(frozen=True, slots=True)
class TrendingRow:
    pair_id: int
    asset: str
    fiat: str
    posts: int
    likes: int
    comments: int


async def trending_pairs(session: AsyncSession, since: datetime, limit: int) -> list[TrendingRow]:
    """Пари з видимих публікацій за період, найобговорюваніші першими."""
    likes, comments, *_ = _counters(None)
    per_post = (
        select(Post.attached_pair_id.label("pair_id"), likes, comments)
        .where(
            Post.status == ContentStatus.VISIBLE,
            Post.created_at >= since,
            Post.attached_pair_id.is_not(None),
        )
        .subquery()
    )
    posts = func.count().label("posts")
    total_likes = func.coalesce(func.sum(per_post.c.likes), 0).label("likes")
    total_comments = func.coalesce(func.sum(per_post.c.comments), 0).label("comments")
    query = (
        select(per_post.c.pair_id, Asset.code, Fiat.code, posts, total_likes, total_comments)
        .select_from(per_post)
        .join(CurrencyPair, CurrencyPair.id == per_post.c.pair_id)
        .join(Asset, Asset.id == CurrencyPair.base_currency_id)
        .join(Fiat, Fiat.id == CurrencyPair.quote_currency_id)
        .group_by(per_post.c.pair_id, Asset.code, Fiat.code)
        .order_by((posts * 3 + total_comments * 2 + total_likes).desc(), Asset.code, Fiat.code)
        .limit(limit)
    )
    return [TrendingRow(*row) for row in (await session.execute(query)).all()]


async def get_post(session: AsyncSession, post_id: int) -> Post | None:
    return await session.get(Post, post_id)


async def get_comment(session: AsyncSession, comment_id: int) -> Comment | None:
    return await session.get(Comment, comment_id)


async def add_post(session: AsyncSession, values: dict[str, Any]) -> int:
    post = Post(**values)
    session.add(post)
    await session.flush()
    return post.id


async def set_post(session: AsyncSession, post_id: int, values: dict[str, Any]) -> None:
    await session.execute(update(Post).where(Post.id == post_id).values(**values))


async def set_comment(session: AsyncSession, comment_id: int, values: dict[str, Any]) -> None:
    await session.execute(update(Comment).where(Comment.id == comment_id).values(**values))


async def like(session: AsyncSession, post_id: int, user_id: int) -> None:
    await session.execute(
        pg_insert(PostLike).values(post_id=post_id, user_id=user_id).on_conflict_do_nothing()
    )


async def unlike(session: AsyncSession, post_id: int, user_id: int) -> None:
    await session.execute(
        delete(PostLike).where(PostLike.post_id == post_id, PostLike.user_id == user_id)
    )


async def repost(session: AsyncSession, post_id: int, user_id: int) -> None:
    await session.execute(
        pg_insert(Repost).values(post_id=post_id, user_id=user_id).on_conflict_do_nothing()
    )


async def unrepost(session: AsyncSession, post_id: int, user_id: int) -> None:
    await session.execute(
        delete(Repost).where(Repost.post_id == post_id, Repost.user_id == user_id)
    )


async def add_comment(session: AsyncSession, post_id: int, author_id: int, text: str) -> int:
    comment = Comment(post_id=post_id, author_id=author_id, text=text)
    session.add(comment)
    await session.flush()
    return comment.id


def _comment_query() -> Select[Comment, str, UserRole]:
    return select(Comment, User.username, User.role).join(User, User.id == Comment.author_id)


def _comment_view(comment: Comment, username: str, role: str) -> CommentView:
    return CommentView(
        id=comment.id,
        post_id=comment.post_id,
        author=_author(comment.author_id, username, role),
        text=comment.text,
        status=comment.status,
        created_at=comment.created_at,
        updated_at=comment.updated_at,
    )


async def comment_views(session: AsyncSession, post_id: int, limit: int) -> list[CommentView]:
    """Видимі коментарі публікації, старші першими."""
    query = (
        _comment_query()
        .where(Comment.post_id == post_id, Comment.status == ContentStatus.VISIBLE)
        .order_by(Comment.created_at, Comment.id)
        .limit(limit)
    )
    return [_comment_view(*row) for row in (await session.execute(query)).all()]


async def comment_view(session: AsyncSession, comment_id: int) -> CommentView | None:
    row = (await session.execute(_comment_query().where(Comment.id == comment_id))).first()
    return None if row is None else _comment_view(*row)


async def user_by_username(session: AsyncSession, username: str) -> User | None:
    query = select(User).where(func.lower(User.username) == username.lower())
    return (await session.execute(query)).scalars().first()


async def profile_view(session: AsyncSession, user: User) -> ProfileView:
    visible = and_(Post.author_id == user.id, Post.status == ContentStatus.VISIBLE)
    posts = select(func.count()).select_from(Post).where(visible).scalar_subquery()
    likes = (
        select(func.count())
        .select_from(PostLike)
        .join(Post, Post.id == PostLike.post_id)
        .where(visible)
        .scalar_subquery()
    )
    reposts = (
        select(func.count())
        .select_from(Repost)
        .join(Post, Post.id == Repost.post_id)
        .where(visible)
        .scalar_subquery()
    )
    counts = (await session.execute(select(posts, likes, reposts))).one()
    return ProfileView(
        id=user.id,
        username=user.username,
        is_admin=user.role == UserRole.ADMIN,
        status=user.status,
        joined_at=user.created_at,
        posts=counts[0],
        likes_received=counts[1],
        reposts_received=counts[2],
    )


async def pending_report_exists(
    session: AsyncSession, reporter_id: int, post_id: int | None, comment_id: int | None
) -> bool:
    query = select(Report.id).where(
        Report.reporter_id == reporter_id,
        Report.status == ReportStatus.PENDING,
        Report.post_id == post_id if post_id is not None else Report.post_id.is_(None),
        Report.comment_id == comment_id if comment_id is not None else Report.comment_id.is_(None),
    )
    return (await session.execute(query.limit(1))).first() is not None


async def add_report(session: AsyncSession, values: dict[str, Any]) -> int:
    report = Report(**values)
    session.add(report)
    await session.flush()
    return report.id


async def report_views(
    session: AsyncSession, status: ReportStatus | None, limit: int, report_id: int | None = None
) -> list[ReportView]:
    """Скарги з текстом і автором цілі; новіші першими (FR-44)."""
    target_author, reporter = aliased(User), aliased(User)
    query = (
        select(
            Report,
            Post.text,
            Post.status,
            Comment.text,
            Comment.status,
            Comment.post_id,
            target_author.id,
            target_author.username,
            target_author.role,
            reporter.username,
            reporter.role,
        )
        .outerjoin(Post, Post.id == Report.post_id)
        .outerjoin(Comment, Comment.id == Report.comment_id)
        .join(target_author, target_author.id == func.coalesce(Post.author_id, Comment.author_id))
        .join(reporter, reporter.id == Report.reporter_id)
        .order_by(Report.created_at.desc(), Report.id.desc())
        .limit(limit)
    )
    if status is not None:
        query = query.where(Report.status == status)
    if report_id is not None:
        query = query.where(Report.id == report_id)
    views = []
    for row in (await session.execute(query)).all():
        (
            report,
            post_text,
            post_status,
            comment_text,
            comment_status,
            comment_post_id,
            author_id,
            author_name,
            author_role,
            reporter_name,
            reporter_role,
        ) = row
        is_post = report.post_id is not None
        views.append(
            ReportView(
                id=report.id,
                target="post" if is_post else "comment",
                post_id=report.post_id if is_post else comment_post_id,
                comment_id=report.comment_id,
                target_text=post_text if is_post else comment_text,
                target_status=post_status if is_post else comment_status,
                target_author=_author(author_id, author_name, author_role),
                reporter=_author(report.reporter_id, reporter_name, reporter_role),
                reason=report.reason if report.reason in REPORT_REASONS else "other",
                details=report.details,
                status=report.status,
                created_at=report.created_at,
                resolved_at=report.resolved_at,
            )
        )
    return views


async def get_report(session: AsyncSession, report_id: int) -> Report | None:
    return await session.get(Report, report_id)


async def resolve_reports(
    session: AsyncSession,
    post_id: int | None,
    comment_id: int | None,
    status: ReportStatus,
    admin_id: int,
) -> int:
    """Закриває всі відкриті скарги на ту саму ціль."""
    target = Report.post_id == post_id if post_id is not None else Report.comment_id == comment_id
    result = await session.execute(
        update(Report)
        .where(target, Report.status == ReportStatus.PENDING)
        .values(status=status, resolved_by_id=admin_id, resolved_at=func.now())
        .returning(Report.id)
    )
    return len(result.all())


async def hidden_post_ids(session: AsyncSession, limit: int) -> list[int]:
    query = (
        select(Post.id)
        .where(Post.status == ContentStatus.HIDDEN)
        .order_by(Post.moderated_at.desc().nulls_last(), Post.id.desc())
        .limit(limit)
    )
    return list((await session.execute(query)).scalars())


async def hidden_comment_views(session: AsyncSession, limit: int) -> list[CommentView]:
    query = (
        _comment_query()
        .where(Comment.status == ContentStatus.HIDDEN)
        .order_by(Comment.moderated_at.desc().nulls_last(), Comment.id.desc())
        .limit(limit)
    )
    return [_comment_view(*row) for row in (await session.execute(query)).all()]


async def add_ban(session: AsyncSession, values: dict[str, Any]) -> int:
    ban = UserBan(**values)
    session.add(ban)
    await session.flush()
    return ban.id


async def lift_bans(session: AsyncSession, user_id: int) -> int:
    result = await session.execute(
        update(UserBan)
        .where(UserBan.user_id == user_id, UserBan.lifted_at.is_(None))
        .values(lifted_at=func.now())
        .returning(UserBan.id)
    )
    return len(result.all())


async def ban_views(
    session: AsyncSession, active_only: bool, now: datetime, ban_id: int | None = None
) -> list[BanView]:
    banned, admin = aliased(User), aliased(User)
    query = (
        select(UserBan, banned.username, banned.role, admin.username, admin.role)
        .join(banned, banned.id == UserBan.user_id)
        .outerjoin(admin, admin.id == UserBan.banned_by_id)
        .order_by(UserBan.created_at.desc())
        .limit(200)
    )
    if ban_id is not None:
        query = query.where(UserBan.id == ban_id)
    if active_only:
        query = query.where(
            UserBan.lifted_at.is_(None), or_(UserBan.until.is_(None), UserBan.until > now)
        )
    return [
        BanView(
            id=ban.id,
            user=_author(ban.user_id, user_name, user_role),
            reason=ban.reason,
            until=ban.until,
            banned_by=(
                None
                if ban.banned_by_id is None
                else _author(ban.banned_by_id, admin_name, admin_role)
            ),
            created_at=ban.created_at,
            lifted_at=ban.lifted_at,
        )
        for ban, user_name, user_role, admin_name, admin_role in (
            await session.execute(query)
        ).all()
    ]


async def moderation_stats(session: AsyncSession, now: datetime) -> ModerationStats:
    def count(model: Any, *conditions: Any) -> Any:
        return select(func.count()).select_from(model).where(*conditions).scalar_subquery()

    pending = Report.status == ReportStatus.PENDING
    week = now - timedelta(days=7)
    deleted = ContentStatus.DELETED
    query = select(
        count(Report, pending, Report.created_at >= now - timedelta(days=1)),
        count(Report, pending),
        count(Post, Post.status == ContentStatus.HIDDEN)
        + count(Comment, Comment.status == ContentStatus.HIDDEN),
        count(Post, Post.status == deleted, Post.moderated_at >= week)
        + count(Comment, Comment.status == deleted, Comment.moderated_at >= week),
        select(func.count(func.distinct(UserBan.user_id)))
        .where(UserBan.lifted_at.is_(None), or_(UserBan.until.is_(None), UserBan.until > now))
        .scalar_subquery(),
    )
    new, pending_total, hidden, deleted_week, banned = (await session.execute(query)).one()
    return ModerationStats(
        reports_new=new,
        reports_pending=pending_total,
        hidden=hidden,
        deleted_7d=deleted_week,
        banned=banned,
    )


async def active_ban(session: AsyncSession, user_id: int, now: datetime) -> UserBan | None:
    """Чинне блокування з найдовшим терміном (безстрокове — першим)."""
    query = (
        select(UserBan)
        .where(
            UserBan.user_id == user_id,
            UserBan.lifted_at.is_(None),
            or_(UserBan.until.is_(None), UserBan.until > now),
        )
        .order_by(UserBan.until.desc().nulls_first())
        .limit(1)
    )
    return (await session.execute(query)).scalars().first()


async def buy_price_change(
    session: AsyncSession, pair_ids: Sequence[int], since: datetime, now: datetime
) -> dict[int, Decimal]:
    """Зміна ціни купівлі за період, %, середня по біржах, де є ціна на початку й наприкінці.

    Береться загальна ціна біржі (без розбивки за способом оплати), усереднена за перші
    й останні PRICE_WINDOW періоду.
    """
    if not pair_ids:
        return {}

    def average(start: datetime, end: datetime) -> Select[Any]:
        return (
            select(MarketStats.pair_id, MarketStats.exchange_id, func.avg(MarketStats.buy_price))
            .where(
                MarketStats.time >= start,
                MarketStats.time < end,
                MarketStats.pair_id.in_(set(pair_ids)),
                MarketStats.payment_method_id.is_(None),
                MarketStats.buy_price.is_not(None),
            )
            .group_by(MarketStats.pair_id, MarketStats.exchange_id)
        )

    first = {
        (pair, exchange): price
        for pair, exchange, price in (
            await session.execute(average(since, since + PRICE_WINDOW))
        ).all()
    }
    changes: dict[int, list[Decimal]] = {}
    for pair, exchange, price in (await session.execute(average(now - PRICE_WINDOW, now))).all():
        start = first.get((pair, exchange))
        if start:
            changes.setdefault(pair, []).append((Decimal(price) - start) / start * 100)
    return {
        pair: (sum(values, Decimal(0)) / len(values)).quantize(Decimal("0.01"))
        for pair, values in changes.items()
    }
