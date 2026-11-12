from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.db import models  # noqa: F401
from app.db.base import Base

EXPECTED_TABLES = {
    "users",
    "refresh_tokens",
    "exchanges",
    "exchange_markets",
    "assets",
    "fiats",
    "payment_methods",
    "payment_method_aliases",
    "currency_pairs",
    "merchants",
    "offer_snapshots",
    "market_stats",
    "spread_history",
    "opportunities",
    "predictions",
    "alert_rules",
    "notifications",
    "watchlist",
    "posts",
    "post_likes",
    "comments",
    "reposts",
    "reports",
    "user_bans",
}
HYPERTABLES = {"offer_snapshots", "market_stats", "spread_history", "opportunities", "predictions"}


def test_all_tables_registered() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_ddl_compiles_for_postgres() -> None:
    dialect = postgresql.dialect()
    for table in Base.metadata.sorted_tables:
        str(CreateTable(table).compile(dialect=dialect))
        for index in table.indexes:
            str(CreateIndex(index).compile(dialect=dialect))


def test_hypertables_have_time_in_primary_key() -> None:
    marked = {name for name, t in Base.metadata.tables.items() if "hypertable" in t.info}
    assert marked == HYPERTABLES
    for name in HYPERTABLES:
        pk = {c.name for c in Base.metadata.tables[name].primary_key.columns}
        assert pk == {"id", "time"}
