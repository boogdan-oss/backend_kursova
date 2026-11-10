"""Події ринку: collector публікує їх у Redis market:updates, API розсилає по WebSocket.

Формат повідомлення — {"type", "ts", "data"}; data завжди містить asset і fiat, за ними
API вирішує, кому з клієнтів подію надіслати.
"""

from typing import Annotated, Literal, get_args

from pydantic import AwareDatetime, BaseModel, Field

from app.domain.market import MarketSnapshot
from app.domain.opportunity import Opportunity

EventType = Literal["snapshot", "opportunity"]
EVENT_TYPES: tuple[EventType, ...] = get_args(EventType)


class SnapshotUpdate(BaseModel):
    """Свіжі ефективні ціни пари на одній біржі: спершу загальна, далі — по способах оплати.

    Ті самі елементи, що віддає /market/snapshot без amount для цієї біржі.
    """

    exchange: str
    asset: str
    fiat: str
    items: list[MarketSnapshot]


class SnapshotEvent(BaseModel):
    type: Literal["snapshot"] = "snapshot"
    ts: AwareDatetime
    data: SnapshotUpdate


class OpportunityEvent(BaseModel):
    """Нова можливість. Повторно, поки вона триває, не надсилається."""

    type: Literal["opportunity"] = "opportunity"
    ts: AwareDatetime
    data: Opportunity


MarketEvent = Annotated[SnapshotEvent | OpportunityEvent, Field(discriminator="type")]
