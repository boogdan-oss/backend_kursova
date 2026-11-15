import pytest

from app.db.models import UserRole
from app.domain.roles import cast_role, is_staff, outranks


@pytest.mark.parametrize(
    ("role", "staff"),
    [(UserRole.USER, False), (UserRole.ADMIN, True), (UserRole.OWNER, True), ("weird", False)],
)
def test_staff(role: str, staff: bool) -> None:
    assert is_staff(role) is staff


def test_rank_order() -> None:
    assert outranks(UserRole.OWNER, UserRole.ADMIN)
    assert outranks(UserRole.ADMIN, UserRole.USER)
    assert not outranks(UserRole.ADMIN, UserRole.ADMIN)
    assert not outranks(UserRole.ADMIN, UserRole.OWNER)
    assert not outranks(UserRole.USER, UserRole.USER)


def test_cast_role() -> None:
    assert [cast_role(role) for role in UserRole] == ["user", "admin", "owner"]
    assert cast_role("unknown") == "user"
