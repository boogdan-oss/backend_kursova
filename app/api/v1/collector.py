from datetime import UTC, datetime

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import SessionDep
from app.collectors.registry import available_exchanges
from app.core.config import get_settings
from app.domain import CollectorStatus
from app.services.market_service import get_collector_status
from app.services.storage_service import GIB, disk_free_bytes

router = APIRouter(tags=["collector"])


class ExchangeCollectorStatus(BaseModel):
    exchange: str
    status: str
    last_ok_at: datetime | None
    pairs_total: int
    pairs_collected: int
    collector_running: bool
    collector: CollectorStatus | None


class TableStatus(BaseModel):
    table: str
    rows: int
    exact: bool
    size_mb: float


class StorageStatus(BaseModel):
    used_gb: float
    budget_gb: float
    per_exchange_gb: float
    disk_free_gb: float
    min_free_gb: float
    full: bool


class CollectorStatusResponse(BaseModel):
    time: datetime
    exchanges: list[ExchangeCollectorStatus]
    tables: list[TableStatus]
    storage: StorageStatus
    merchants: int


@router.get(
    "/collector/status",
    response_model=CollectorStatusResponse,
    summary="Чи працює збір і чи записуються дані в БД",
    description=(
        "collector — поточна швидкість rate limiter і лічильники від запуску (з Redis; "
        "null — collector не працює). tables — скільки рядків уже в БД: виклич двічі, і "
        "кількість має зростати.\n\nstorage — скільки місця займають дані і бюджет: понад "
        "нього, а також коли на диску лишається менше min_free_gb, collector видаляє "
        "найстаріші дні сирих оголошень."
    ),
)
async def collector_status(session: SessionDep) -> CollectorStatusResponse:
    view = await get_collector_status(session)
    settings = get_settings()
    exchanges = len(set(settings.exchanges) & set(available_exchanges()))
    used_gb = sum(table.size_mb for table in view.tables) / 1024
    budget_gb = settings.collector_storage_gb_per_exchange * exchanges
    disk_free_gb = disk_free_bytes() / GIB
    min_free_gb = settings.collector_min_free_disk_gb
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
        storage=StorageStatus(
            used_gb=round(used_gb, 2),
            budget_gb=budget_gb,
            per_exchange_gb=settings.collector_storage_gb_per_exchange,
            disk_free_gb=round(disk_free_gb, 2),
            min_free_gb=min_free_gb,
            full=used_gb > budget_gb or disk_free_gb < min_free_gb,
        ),
        merchants=view.merchants,
    )
