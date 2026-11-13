"""Crypto Community (FR-31 – FR-43): стрічка, публікації, лайки, репости, коментарі, профілі,
скарги. Читати може будь-хто, писати — лише ті, хто увійшов і не заблокований."""

from collections.abc import Awaitable
from typing import Annotated, Any, NoReturn, TypeVar

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator

from app.api.deps import CODE_PATTERN, CurrentUser, OptionalUser, SessionDep
from app.domain import (
    CommentView,
    FeedItem,
    PairRef,
    PostView,
    ProfileView,
    ReportReason,
    ReportView,
    TrendingPair,
)
from app.services import community_service as service
from app.services.community_service import (
    BadRequest,
    CommunityError,
    Conflict,
    FeedScope,
    FeedSort,
    Forbidden,
    NotFound,
    ProfileTab,
)

router = APIRouter(prefix="/community", tags=["community"])

T = TypeVar("T")

POST_MAX = 2000
COMMENT_MAX = 1000

PostText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=POST_MAX)
]
CommentText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=COMMENT_MAX)
]
Code = Annotated[str, Field(pattern=CODE_PATTERN), AfterValidator(str.upper)]
Limit = Annotated[int, Query(ge=1, le=50, description="Скільки елементів на сторінці")]
Cursor = Annotated[
    str | None,
    Query(max_length=200, description="next_cursor з попередньої сторінки, як є"),
]

STATUS_BY_ERROR: dict[type[CommunityError], int] = {
    NotFound: status.HTTP_404_NOT_FOUND,
    Forbidden: status.HTTP_403_FORBIDDEN,
    Conflict: status.HTTP_409_CONFLICT,
    BadRequest: status.HTTP_400_BAD_REQUEST,
}


def raise_http(exc: CommunityError) -> NoReturn:
    raise HTTPException(STATUS_BY_ERROR.get(type(exc), status.HTTP_400_BAD_REQUEST), str(exc))


async def handled(call: Awaitable[T]) -> T:
    """Помилки сервісу → HTTP-статуси: 404, 403, 409, 400."""
    try:
        return await call
    except CommunityError as exc:
        raise_http(exc)


class PairIn(BaseModel):
    asset: Code
    fiat: Code

    def ref(self) -> PairRef:
        return PairRef(asset=self.asset, fiat=self.fiat)


class PostIn(BaseModel):
    text: PostText
    pair: PairIn | None = Field(default=None, description="Прикріплена пара (FR-42)")
    opportunity_id: int | None = Field(
        default=None, ge=1, description="id з /opportunities/history (FR-42)"
    )


class PostPatch(BaseModel):
    """Змінюються лише передані поля; null для pair чи opportunity_id прибирає прикріплення."""

    text: PostText | None = None
    pair: PairIn | None = None
    opportunity_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def text_not_null(self) -> "PostPatch":
        if "text" in self.model_fields_set and self.text is None:
            raise ValueError("Текст публікації не може бути порожнім")
        return self

    def changes(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for name in self.model_fields_set:
            value = getattr(self, name)
            values[name] = value.ref() if isinstance(value, PairIn) else value
        return values


class CommentIn(BaseModel):
    text: CommentText


class ReportIn(BaseModel):
    post_id: int | None = Field(default=None, ge=1)
    comment_id: int | None = Field(default=None, ge=1)
    reason: ReportReason
    details: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=1000)] = None

    @model_validator(mode="after")
    def single_target(self) -> "ReportIn":
        if (self.post_id is None) == (self.comment_id is None):
            raise ValueError("Вкажіть або post_id, або comment_id")
        return self


class FeedPage(BaseModel):
    items: list[FeedItem]
    next_cursor: str | None = Field(description="null — сторінок більше немає")


class CommentsResponse(BaseModel):
    count: int
    items: list[CommentView]


class TrendingResponse(BaseModel):
    items: list[TrendingPair]


def _page(page: service.FeedPage) -> FeedPage:
    return FeedPage(items=page.items, next_cursor=page.next_cursor)


@router.get(
    "/feed",
    response_model=FeedPage,
    summary="Стрічка спільноти (FR-31)",
    description=(
        "sort=new — публікації й репости, новіші першими; sort=popular — публікації за 7 днів "
        "за балами (лайк 1, коментар 2, репост 3). scope=mine — лише мої публікації й репости "
        "(потрібен вхід). asset + fiat — лише публікації з цією парою."
    ),
)
async def get_feed(
    session: SessionDep,
    viewer: OptionalUser,
    sort: FeedSort = "new",
    scope: FeedScope = "all",
    asset: Annotated[str | None, Query(pattern=CODE_PATTERN)] = None,
    fiat: Annotated[str | None, Query(pattern=CODE_PATTERN)] = None,
    cursor: Cursor = None,
    limit: Limit = 20,
) -> FeedPage:
    if (asset is None) != (fiat is None):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Вкажіть asset разом із fiat")
    if scope == "mine" and viewer is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Потрібно увійти")
    pair = (
        None if asset is None or fiat is None else PairRef(asset=asset.upper(), fiat=fiat.upper())
    )
    page = await handled(service.feed(session, viewer, sort, scope, pair, cursor, limit))
    return _page(page)


@router.get(
    "/trending-pairs",
    response_model=TrendingResponse,
    summary="Найобговорюваніші пари",
    description=(
        "Пари з видимих публікацій за період. change_pct — зміна ціни купівлі за той самий "
        "період, середня по біржах; null — немає даних."
    ),
)
async def trending_pairs(
    session: SessionDep,
    hours: Annotated[int, Query(ge=1, le=168)] = 24,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> TrendingResponse:
    return TrendingResponse(items=await service.trending(session, hours, limit))


@router.post(
    "/posts",
    response_model=PostView,
    status_code=status.HTTP_201_CREATED,
    summary="Нова публікація (FR-32, FR-42)",
)
async def create_post(body: PostIn, session: SessionDep, user: CurrentUser) -> PostView:
    pair = None if body.pair is None else body.pair.ref()
    return await handled(service.create_post(session, user, body.text, pair, body.opportunity_id))


@router.get("/posts/{post_id}", response_model=PostView, summary="Публікація")
async def get_post(post_id: int, session: SessionDep, viewer: OptionalUser) -> PostView:
    return await handled(service.get_post(session, post_id, viewer))


@router.patch("/posts/{post_id}", response_model=PostView, summary="Редагувати свою (FR-33)")
async def edit_post(
    post_id: int, body: PostPatch, session: SessionDep, user: CurrentUser
) -> PostView:
    return await handled(service.edit_post(session, user, post_id, body.changes()))


@router.delete(
    "/posts/{post_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Видалити свою (FR-34)",
)
async def delete_post(post_id: int, session: SessionDep, user: CurrentUser) -> Response:
    await handled(service.delete_post(session, user, post_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/posts/{post_id}/like", response_model=PostView, summary="Лайк (FR-35)")
async def like(post_id: int, session: SessionDep, user: CurrentUser) -> PostView:
    return await handled(service.set_like(session, user, post_id, True))


@router.delete("/posts/{post_id}/like", response_model=PostView, summary="Скасувати лайк")
async def unlike(post_id: int, session: SessionDep, user: CurrentUser) -> PostView:
    return await handled(service.set_like(session, user, post_id, False))


@router.put("/posts/{post_id}/repost", response_model=PostView, summary="Репост (FR-38)")
async def repost(post_id: int, session: SessionDep, user: CurrentUser) -> PostView:
    return await handled(service.set_repost(session, user, post_id, True))


@router.delete(
    "/posts/{post_id}/repost", response_model=PostView, summary="Скасувати репост (FR-39)"
)
async def unrepost(post_id: int, session: SessionDep, user: CurrentUser) -> PostView:
    return await handled(service.set_repost(session, user, post_id, False))


@router.get(
    "/posts/{post_id}/comments",
    response_model=CommentsResponse,
    summary="Коментарі, старші першими",
)
async def list_comments(
    post_id: int,
    session: SessionDep,
    viewer: OptionalUser,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> CommentsResponse:
    items = await handled(service.comments(session, post_id, viewer, limit))
    return CommentsResponse(count=len(items), items=items)


@router.post(
    "/posts/{post_id}/comments",
    response_model=CommentView,
    status_code=status.HTTP_201_CREATED,
    summary="Коментар (FR-36)",
)
async def add_comment(
    post_id: int, body: CommentIn, session: SessionDep, user: CurrentUser
) -> CommentView:
    return await handled(service.add_comment(session, user, post_id, body.text))


@router.patch(
    "/comments/{comment_id}", response_model=CommentView, summary="Редагувати свій (FR-37)"
)
async def edit_comment(
    comment_id: int, body: CommentIn, session: SessionDep, user: CurrentUser
) -> CommentView:
    return await handled(service.edit_comment(session, user, comment_id, body.text))


@router.delete(
    "/comments/{comment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Видалити свій (FR-37)",
)
async def delete_comment(comment_id: int, session: SessionDep, user: CurrentUser) -> Response:
    await handled(service.delete_comment(session, user, comment_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/users/{username}",
    response_model=ProfileView,
    summary="Профіль користувача (FR-40)",
    description="Без email. Адміністратор бачить ще чинне блокування (ban_until, ban_reason).",
)
async def get_profile(username: str, session: SessionDep, viewer: OptionalUser) -> ProfileView:
    return await handled(service.profile(session, viewer, username))


@router.get(
    "/users/{username}/feed",
    response_model=FeedPage,
    summary="Публікації, репости або вподобане користувача (FR-40)",
)
async def get_user_feed(
    username: str,
    session: SessionDep,
    viewer: OptionalUser,
    tab: ProfileTab = "posts",
    cursor: Cursor = None,
    limit: Limit = 20,
) -> FeedPage:
    return _page(await handled(service.user_feed(session, viewer, username, tab, cursor, limit)))


@router.post(
    "/reports",
    response_model=ReportView,
    status_code=status.HTTP_201_CREATED,
    summary="Скарга на публікацію чи коментар (FR-43)",
    description="Повторна скарга на ту саму ціль, поки попередня на розгляді, — 409.",
)
async def create_report(body: ReportIn, session: SessionDep, user: CurrentUser) -> ReportView:
    return await handled(
        service.report(
            session, user, body.post_id, body.comment_id, body.reason, body.details or None
        )
    )
