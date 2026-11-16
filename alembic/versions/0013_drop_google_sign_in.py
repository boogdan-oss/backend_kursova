"""Вхід через Google прибрано: колонка users.google_sub більше не потрібна.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-16

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_users_google_sub", "users", type_="unique")
    op.drop_column("users", "google_sub")


def downgrade() -> None:
    op.add_column("users", sa.Column("google_sub", sa.String(64), nullable=True))
    op.create_unique_constraint("uq_users_google_sub", "users", ["google_sub"])
