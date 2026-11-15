"""Роль owner (власник): усі права admin і керування ролями.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-14

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_users_userrole"


def upgrade() -> None:
    op.drop_constraint(CONSTRAINT, "users", type_="check")
    op.create_check_constraint(CONSTRAINT, "users", "role IN ('user', 'admin', 'owner')")


def downgrade() -> None:
    op.execute("UPDATE users SET role = 'admin' WHERE role = 'owner'")
    op.drop_constraint(CONSTRAINT, "users", type_="check")
    op.create_check_constraint(CONSTRAINT, "users", "role IN ('user', 'admin')")
