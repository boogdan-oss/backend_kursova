from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, status
from pydantic import AfterValidator, AwareDatetime, BaseModel, Field, ValidationError

from app.api.deps import CODE_PATTERN, CurrentUser, SessionDep, ensure_exchange, pair_not_found
from app.db.repositories.alerts import RuleRow, list_notifications, mark_read
from app.domain import NotificationChannel, Opportunity
from app.services.alert_service import (
    AlertError,
    LimitReached,
    RuleNotFound,
    add_rule,
    change_rule,
    get_rules,
    remove_rule,
)
from app.services.market_service import PairNotFound

router = APIRouter(tags=["alerts"])

Code = Annotated[str, Field(pattern=CODE_PATTERN), AfterValidator(str.upper)]
Percent = Annotated[Decimal, Field(ge=-100, le=1000)]
Channels = Annotated[list[NotificationChannel], Field(min_length=1, max_length=3)]


class RuleFields(BaseModel):
    title: Annotated[str | None, Field(max_length=100)] = None
    exchange: str | None = Field(default=None, description="Купівля або продаж на цій біржі")
    payment: Annotated[str | None, Field(max_length=64, description="Спосіб оплати")] = None
    min_spread: Percent | None = None
    min_net_spread: Percent | None = None
    min_ai_score: Annotated[int | None, Field(ge=0, le=100)] = None
    min_rr: Annotated[Decimal | None, Field(ge=0)] = None
    min_net_profit: Annotated[Decimal | None, Field(description="У фіаті пари")] = None


class RuleIn(RuleFields):
    asset: Code
    fiat: Code
    channels: Channels = ["in_app"]
    enabled: bool = True


class RulePatch(RuleFields):
    asset: Code | None = None
    fiat: Code | None = None
    channels: Channels | None = None
    enabled: bool | None = None


class RuleOut(BaseModel):
    id: int
    title: str | None
    asset: str
    fiat: str
    exchange: str | None
    payment: str | None
    min_spread: Decimal | None
    min_net_spread: Decimal | None
    min_ai_score: int | None
    min_rr: Decimal | None
    min_net_profit: Decimal | None
    channels: list[NotificationChannel]
    enabled: bool
    created_at: AwareDatetime
    last_triggered_at: AwareDatetime | None
    triggered_today: int

    @classmethod
    def of(cls, row: RuleRow) -> "RuleOut":
        return cls(
            **row.rule.model_dump(exclude={"id", "user_id"}),
            id=row.rule.id,
            created_at=row.created_at,
            last_triggered_at=row.last_triggered_at,
            triggered_today=row.triggered_today,
        )


class RulesResponse(BaseModel):
    count: int
    items: list[RuleOut]


class NotificationOut(BaseModel):
    id: int
    rule_id: int | None
    rule_title: str | None
    text: str
    channels: list[NotificationChannel]
    delivery: dict[str, str] = Field(
        description="Результат по каналах: sent, failed, skipped: <причина>"
    )
    opportunity: Opportunity | None
    read: bool
    created_at: AwareDatetime


class NotificationsResponse(BaseModel):
    count: int
    unread: int
    items: list[NotificationOut]


class MarkReadRequest(BaseModel):
    ids: list[int] | None = Field(default=None, description="Без ids — позначити всі")


class MarkReadResponse(BaseModel):
    updated: int


def _alert_error(exc: Exception) -> HTTPException:
    if isinstance(exc, PairNotFound):
        return pair_not_found(exc)
    if isinstance(exc, RuleNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, LimitReached):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


@router.get("/alerts/rules", response_model=RulesResponse, summary="Мої правила сповіщень")
async def list_rules(user: CurrentUser, session: SessionDep) -> RulesResponse:
    rows = await get_rules(session, user)
    return RulesResponse(count=len(rows), items=[RuleOut.of(row) for row in rows])


@router.post(
    "/alerts/rules",
    response_model=RuleOut,
    status_code=status.HTTP_201_CREATED,
    summary="Створити правило сповіщення",
    description=(
        "Правило спрацьовує на кожну нову можливість пари, що досягла всіх заданих порогів "
        "(хоча б один обов'язковий). Канали: in_app (сайт, WebSocket-подія alert), telegram "
        "(спершу POST /me/telegram), email (якщо на сервері налаштовано SMTP)."
    ),
)
async def create(body: RuleIn, user: CurrentUser, session: SessionDep) -> RuleOut:
    if body.exchange is not None:
        ensure_exchange(body.exchange)
    try:
        row = await add_rule(session, user, body.model_dump())
    except (AlertError, PairNotFound) as exc:
        raise _alert_error(exc) from exc
    return RuleOut.of(row)


@router.patch(
    "/alerts/rules/{rule_id}",
    response_model=RuleOut,
    summary="Змінити правило",
    description="Змінюються лише передані поля; null у порозі прибирає поріг.",
)
async def patch(
    rule_id: Annotated[int, Path(ge=1)], body: RulePatch, user: CurrentUser, session: SessionDep
) -> RuleOut:
    changes: dict[str, Any] = body.model_dump(exclude_unset=True)
    for field in ("asset", "fiat", "channels", "enabled"):
        if field in changes and changes[field] is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{field} не може бути null")
    if changes.get("exchange") is not None:
        ensure_exchange(changes["exchange"])
    try:
        row = await change_rule(session, user, rule_id, changes)
    except (AlertError, PairNotFound) as exc:
        raise _alert_error(exc) from exc
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return RuleOut.of(row)


@router.delete(
    "/alerts/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Видалити правило"
)
async def delete(
    rule_id: Annotated[int, Path(ge=1)], user: CurrentUser, session: SessionDep
) -> None:
    if not await remove_rule(session, user, rule_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Правило не знайдено")


@router.get(
    "/notifications",
    response_model=NotificationsResponse,
    summary="Історія сповіщень, новіші першими",
)
async def notifications(
    user: CurrentUser,
    session: SessionDep,
    unread_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> NotificationsResponse:
    rows, total, unread = await list_notifications(session, user.id, unread_only, limit)
    return NotificationsResponse(
        count=total,
        unread=unread,
        items=[
            NotificationOut(
                id=row.id,
                rule_id=row.alert_rule_id,
                rule_title=title,
                text=row.text,
                channels=row.channels,
                delivery=row.delivery,
                opportunity=Opportunity.model_validate(row.data) if row.data else None,
                read=row.read,
                created_at=row.sent_at,
            )
            for row, title in rows
        ],
    )


@router.post(
    "/notifications/read",
    response_model=MarkReadResponse,
    summary="Позначити сповіщення прочитаними",
)
async def read_notifications(
    body: MarkReadRequest, user: CurrentUser, session: SessionDep
) -> MarkReadResponse:
    updated = await mark_read(session, user.id, body.ids)
    await session.commit()
    return MarkReadResponse(updated=updated)
