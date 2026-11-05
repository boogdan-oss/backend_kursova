"""Початкова схема: довідники, users, merchants, offer_snapshots і market_stats (hypertables).

Revision ID: 0001
Revises:
Create Date: 2026-11-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

Money = sa.Numeric(28, 8)
Percent = sa.Numeric(12, 4)
TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    # --- Довідники ---
    op.create_table(
        "exchanges",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("api_url", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("last_ok_at", TZ, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_exchanges"),
        sa.UniqueConstraint("code", name="uq_exchanges_code"),
        sa.CheckConstraint(
            "status IN ('active', 'degraded', 'disabled')", name="ck_exchanges_exchangestatus"
        ),
    )
    op.create_table(
        "assets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_assets"),
        sa.UniqueConstraint("code", name="uq_assets_code"),
    )
    op.create_table(
        "fiats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(8), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_fiats"),
        sa.UniqueConstraint("code", name="uq_fiats_code"),
    )
    op.create_table(
        "payment_methods",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("fiat_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_payment_methods"),
        sa.UniqueConstraint("code", name="uq_payment_methods_code"),
        sa.ForeignKeyConstraint(
            ["fiat_id"], ["fiats.id"], name="fk_payment_methods_fiat_id_fiats", ondelete="SET NULL"
        ),
    )
    op.create_table(
        "payment_method_aliases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("exchange_id", sa.Integer(), nullable=False),
        sa.Column("payment_method_id", sa.Integer(), nullable=False),
        sa.Column("alias", sa.String(128), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_payment_method_aliases"),
        sa.UniqueConstraint(
            "exchange_id", "alias", name="uq_payment_method_aliases_exchange_id_alias"
        ),
        sa.ForeignKeyConstraint(
            ["exchange_id"],
            ["exchanges.id"],
            name="fk_payment_method_aliases_exchange_id_exchanges",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["payment_method_id"],
            ["payment_methods.id"],
            name="fk_payment_method_aliases_payment_method_id_payment_methods",
            ondelete="CASCADE",
        ),
    )
    op.create_table(
        "currency_pairs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("base_currency_id", sa.Integer(), nullable=False),
        sa.Column("quote_currency_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_currency_pairs"),
        sa.UniqueConstraint(
            "base_currency_id",
            "quote_currency_id",
            name="uq_currency_pairs_base_currency_id_quote_currency_id",
        ),
        sa.ForeignKeyConstraint(
            ["base_currency_id"],
            ["assets.id"],
            name="fk_currency_pairs_base_currency_id_assets",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["quote_currency_id"],
            ["fiats.id"],
            name="fk_currency_pairs_quote_currency_id_fiats",
            ondelete="RESTRICT",
        ),
    )

    # --- Користувачі ---
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(32), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("settings", postgresql.JSONB(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("telegram_chat_id", sa.String(64), nullable=True),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.CheckConstraint("role IN ('user', 'admin')", name="ck_users_userrole"),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'banned')", name="ck_users_userstatus"
        ),
    )

    # --- Продавці ---
    op.create_table(
        "merchants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("exchange_id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("nickname", sa.String(128), nullable=False),
        sa.Column("orders_30d", sa.Integer(), nullable=False),
        sa.Column("completion_rate", sa.Numeric(5, 4), nullable=False),
        sa.Column("updated_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_merchants"),
        sa.UniqueConstraint(
            "exchange_id", "external_id", name="uq_merchants_exchange_id_external_id"
        ),
        sa.CheckConstraint(
            "completion_rate BETWEEN 0 AND 1", name="ck_merchants_completion_rate_range"
        ),
        sa.ForeignKeyConstraint(
            ["exchange_id"],
            ["exchanges.id"],
            name="fk_merchants_exchange_id_exchanges",
            ondelete="CASCADE",
        ),
    )

    # --- Часові ряди (hypertables: PK складений (id, time)) ---
    op.create_table(
        "offer_snapshots",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("time", TZ, nullable=False),
        sa.Column("exchange_id", sa.Integer(), nullable=False),
        sa.Column("pair_id", sa.Integer(), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("price", Money, nullable=False),
        sa.Column("min_amount", Money, nullable=False),
        sa.Column("max_amount", Money, nullable=False),
        sa.Column("available", Money, nullable=False),
        sa.Column("payment_methods", postgresql.ARRAY(sa.String(64)), nullable=False),
        sa.Column("merchant_id", sa.Integer(), nullable=True),
        sa.Column("external_id", sa.String(128), nullable=True),
        sa.PrimaryKeyConstraint("id", "time", name="pk_offer_snapshots"),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name="ck_offer_snapshots_side"),
        sa.ForeignKeyConstraint(
            ["exchange_id"], ["exchanges.id"], name="fk_offer_snapshots_exchange_id_exchanges"
        ),
        sa.ForeignKeyConstraint(
            ["pair_id"], ["currency_pairs.id"], name="fk_offer_snapshots_pair_id_currency_pairs"
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], name="fk_offer_snapshots_merchant_id_merchants"
        ),
    )
    op.create_index(
        "ix_offer_snapshots_pair_side_time",
        "offer_snapshots",
        ["pair_id", "side", sa.text("time DESC")],
    )

    op.create_table(
        "market_stats",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("time", TZ, nullable=False),
        sa.Column("exchange_id", sa.Integer(), nullable=False),
        sa.Column("pair_id", sa.Integer(), nullable=False),
        sa.Column("payment_method_id", sa.Integer(), nullable=True),
        sa.Column("buy_price", Money, nullable=True),
        sa.Column("sell_price", Money, nullable=True),
        sa.Column("spread", Percent, nullable=True),
        sa.Column("volume", Money, nullable=False),
        sa.Column("offers_count_buy", sa.Integer(), nullable=False),
        sa.Column("offers_count_sell", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", "time", name="pk_market_stats"),
        sa.ForeignKeyConstraint(
            ["exchange_id"], ["exchanges.id"], name="fk_market_stats_exchange_id_exchanges"
        ),
        sa.ForeignKeyConstraint(
            ["pair_id"], ["currency_pairs.id"], name="fk_market_stats_pair_id_currency_pairs"
        ),
        sa.ForeignKeyConstraint(
            ["payment_method_id"],
            ["payment_methods.id"],
            name="fk_market_stats_payment_method_id_payment_methods",
        ),
    )
    op.create_index(
        "ix_market_stats_pair_exchange_time",
        "market_stats",
        ["pair_id", "exchange_id", sa.text("time DESC")],
    )

    # create_default_indexes => FALSE: індекси по time вже покриті нашими складеними
    op.execute(
        "SELECT create_hypertable('offer_snapshots', 'time', "
        "chunk_time_interval => INTERVAL '1 day', create_default_indexes => FALSE)"
    )
    op.execute(
        "SELECT create_hypertable('market_stats', 'time', "
        "chunk_time_interval => INTERVAL '7 days', create_default_indexes => FALSE)"
    )

    # offer_snapshots — найоб'ємніша таблиця: стиснення старших за 7 днів, retention 30 днів
    op.execute(
        "ALTER TABLE offer_snapshots SET ("
        "timescaledb.compress, "
        "timescaledb.compress_segmentby = 'exchange_id, pair_id, side', "
        "timescaledb.compress_orderby = 'time DESC')"
    )
    op.execute("SELECT add_compression_policy('offer_snapshots', INTERVAL '7 days')")
    op.execute("SELECT add_retention_policy('offer_snapshots', INTERVAL '30 days')")


def downgrade() -> None:
    # Політики й чанки TimescaleDB видаляються разом із hypertable
    op.drop_table("market_stats")
    op.drop_table("offer_snapshots")
    op.drop_table("merchants")
    op.drop_table("users")
    op.drop_table("currency_pairs")
    op.drop_table("payment_method_aliases")
    op.drop_table("payment_methods")
    op.drop_table("fiats")
    op.drop_table("assets")
    op.drop_table("exchanges")
    op.execute("DROP EXTENSION IF EXISTS timescaledb")
