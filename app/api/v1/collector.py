from datetime import UTC, datetime

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import SessionDep
from app.domain import CollectorStatus
from app.services.market_service import get_collector_status

router = APIRouter(tags=["collector"])


class ExchangeCollectorStatus(BaseModel):
    exchange: str
    status: str
    last_ok_at: datetime | None
    pairs_total: int
    pairs_collected: int  # пари, які вже збирали хоча б раз
    collector_running: bool
    collector: CollectorStatus | None


class TableStatus(BaseModel):
    table: str
    rows: int
    exact: bool  # false — оцінка TimescaleDB для великої таблиці
    size_mb: float


class CollectorStatusResponse(BaseModel):
    time: datetime
    exchanges: list[ExchangeCollectorStatus]
    tables: list[TableStatus]
    merchants: int


@router.get(
    "/collector/status",
    response_model=CollectorStatusResponse,
    summary="Чи працює збір і чи записуються дані в БД",
    description=(
        "collector — поточна швидкість rate limiter і лічильники від запуску (з Redis; "
        "null — collector не працює). tables — скільки рядків уже в БД: виклич двічі, і "
        "кількість має зростати."
    ),
)
async def collector_status(session: SessionDep) -> CollectorStatusResponse:
    view = await get_collector_status(session)
    return CollectorStatusResponse(
        time=datetime.now(UTC),
        exchanges=[
            ExchangeCollectorStatus(
                exchange=item.overview.exchange,
                status=item.overview.status,
                last_ok_at=item.overview.last_ok_at,
                pairs_total=item.overview.pairs_total,
                pairs_collected=item.overview.pairs_collected,
                collector_running=item.collector is not None,
                collector=item.collector,
            )
            for item in view.exchanges
        ],
        tables=[
            TableStatus(table=t.table, rows=t.rows, exact=t.exact, size_mb=t.size_mb)
            for t in view.tables
        ],
        merchants=view.merchants,
    )
