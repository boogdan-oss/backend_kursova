"""exchange_markets: пари на біржах і свіжість даних (CLAUDE.md, розділ 5.1).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "exchange_markets",
        sa.Column("exchange_id", sa.Integer(), nullable=False),
        sa.Column("pair_id", sa.Integer(), nullable=False),
        sa.Column("payment_methods", postgresql.ARRAY(sa.String(64)), nullable=False),
        sa.Column("listed_at", TZ, nullable=True),
        sa.Column("buy_fetched_at", TZ, nullable=True),
        sa.Column("sell_fetched_at", TZ, nullable=True),
        sa.PrimaryKeyConstraint("exchange_id", "pair_id", name="pk_exchange_markets"),
        sa.ForeignKeyConstraint(
            ["exchange_id"],
            ["exchanges.id"],
            name="fk_exchange_markets_exchange_id_exchanges",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["pair_id"],
            ["currency_pairs.id"],
            name="fk_exchange_markets_pair_id_currency_pairs",
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table("exchange_markets")
