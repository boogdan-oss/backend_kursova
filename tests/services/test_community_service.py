from datetime import UTC, datetime

import pytest

from app.db.repositories.community import FeedRow
from app.services.auth_service import blocked_message
from app.services.community_service import (
    BadRequest,
    _encode,
    parse_offset_cursor,
    parse_timeline_cursor,
    timeline_cursor,
)

AT = datetime(2026, 11, 13, 10, 30, 15, 123456, tzinfo=UTC)


@pytest.mark.parametrize("reposter", [None, 7])
def test_timeline_cursor_round_trip(reposter: int | None) -> None:
    row = FeedRow(42, reposter, AT)

    assert parse_timeline_cursor(timeline_cursor(row)) == row
    assert parse_timeline_cursor(None) is None


@pytest.mark.parametrize(
    "cursor",
    ["nonsense", _encode("t|not-a-date|1|0"), _encode("t|2026-11-13T10:30:15|1|0"), _encode("o|5")],
)
def test_bad_timeline_cursor_is_rejected(cursor: str) -> None:
    with pytest.raises(BadRequest):
        parse_timeline_cursor(cursor)


def test_offset_cursor() -> None:
    assert parse_offset_cursor(None) == 0
    assert parse_offset_cursor(_encode("o|40")) == 40
    with pytest.raises(BadRequest):
        parse_offset_cursor(_encode("o|-1"))


def test_blocked_message_shows_kyiv_time_and_reason() -> None:
    assert blocked_message(AT, "Спам") == (
        "Обліковий запис заблоковано до 13.11.2026 12:30 (за Києвом). Причина: Спам"
    )
    assert blocked_message(None, None) == "Обліковий запис заблоковано назавжди"
