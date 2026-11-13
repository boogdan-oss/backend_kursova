"""Модерація спільноти (FR-44 – FR-46): скарги, приховування й відновлення, блокування.
Лише для role=admin; іншим — 403."""

from typing import Annotated

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, Field, StringConstraints

from app.api.deps import AdminUser, SessionDep
from app.api.v1.community import handled
from app.db.models import ContentStatus, ReportStatus
from app.domain import BanView, CommentView, ModerationStats, PostView, ReportView
from app.services import community_service as service
from app.services.community_service import ReportAction

router = APIRouter(prefix="/admin", tags=["admin"])

AdminLimit = Annotated[int, Query(ge=1, le=500)]


class ReportsResponse(BaseModel):
    count: int
    items: list[ReportView]


class ResolveIn(BaseModel):
    action: ReportAction = Field(
        description="hide / delete — змінити статус цілі (скарги підтверджено), dismiss — відхилити"
    )


class StatusIn(BaseModel):
    status: ContentStatus = Field(description="visible — відновити (FR-46)")


class HiddenResponse(BaseModel):
    posts: list[PostView]
    comments: list[CommentView]


class BansResponse(BaseModel):
    count: int
    items: list[BanView]


class BanIn(BaseModel):
    user_id: int = Field(ge=1)
    days: int | None = Field(default=None, ge=1, le=3650, description="null — назавжди")
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


@router.get("/stats", response_model=ModerationStats, summary="Лічильники модерації")
async def stats(session: SessionDep, _: AdminUser) -> ModerationStats:
    return await service.moderation_stats(session)


@router.get(
    "/reports",
    response_model=ReportsResponse,
    summary="Скарги, новіші першими (FR-44)",
)
async def list_reports(
    session: SessionDep,
    _: AdminUser,
    status_filter: Annotated[ReportStatus | None, Query(alias="status")] = None,
    limit: AdminLimit = 100,
) -> ReportsResponse:
    items = await service.reports(session, status_filter, limit)
    return ReportsResponse(count=len(items), items=items)


@router.post(
    "/reports/{report_id}/resolve",
    response_model=ReportView,
    summary="Рішення за скаргою",
    description="Рішення стосується цілі, тож закриває всі відкриті скарги на неї.",
)
async def resolve_report(
    report_id: int, body: ResolveIn, session: SessionDep, admin: AdminUser
) -> ReportView:
    return await handled(service.resolve_report(session, admin, report_id, body.action))


@router.put(
    "/posts/{post_id}/status",
    response_model=PostView,
    summary="Приховати, видалити чи відновити публікацію (FR-44, FR-46)",
)
async def set_post_status(
    post_id: int, body: StatusIn, session: SessionDep, admin: AdminUser
) -> PostView:
    return await handled(service.set_post_status(session, admin, post_id, body.status))


@router.put(
    "/comments/{comment_id}/status",
    response_model=CommentView,
    summary="Приховати, видалити чи відновити коментар (FR-44, FR-46)",
)
async def set_comment_status(
    comment_id: int, body: StatusIn, session: SessionDep, admin: AdminUser
) -> CommentView:
    return await handled(service.set_comment_status(session, admin, comment_id, body.status))


@router.get("/hidden", response_model=HiddenResponse, summary="Приховане модерацією")
async def hidden(session: SessionDep, admin: AdminUser, limit: AdminLimit = 100) -> HiddenResponse:
    posts, comments = await service.hidden(session, admin, limit)
    return HiddenResponse(posts=posts, comments=comments)


@router.get("/bans", response_model=BansResponse, summary="Блокування (FR-45)")
async def list_bans(session: SessionDep, _: AdminUser, active: bool = True) -> BansResponse:
    items = await service.bans(session, active)
    return BansResponse(count=len(items), items=items)


@router.post(
    "/bans",
    response_model=BanView,
    status_code=status.HTTP_201_CREATED,
    summary="Заблокувати користувача (FR-45)",
    description=(
        "days — тимчасово (статус suspended, знімається сам), null — назавжди (banned). "
        "Нове блокування замінює чинне; усі сесії користувача відкликаються."
    ),
)
async def ban_user(body: BanIn, session: SessionDep, admin: AdminUser) -> BanView:
    return await handled(service.ban(session, admin, body.user_id, body.days, body.reason))


@router.delete(
    "/bans/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Розблокувати користувача",
)
async def unban_user(user_id: int, session: SessionDep, admin: AdminUser) -> Response:
    await handled(service.unban(session, admin, user_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
