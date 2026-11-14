"""Профіль користувача: ім'я для показу й короткий опис.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-14

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("display_name", sa.String(64), nullable=True))
    op.add_column("users", sa.Column("bio", sa.String(300), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "bio")
    op.drop_column("users", "display_name")
