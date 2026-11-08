import io
import re
from pathlib import Path

from alembic.config import Config

from alembic import command
from app.db import models  # noqa: F401
from app.db.base import Base

BACKEND_DIR = Path(__file__).resolve().parents[2]


def render_upgrade_sql() -> str:
    """SQL міграцій в офлайн-режимі (alembic upgrade head --sql) — без підключення до БД."""
    buffer = io.StringIO()
    config = Config(str(BACKEND_DIR / "alembic.ini"), output_buffer=buffer)
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(config, "head", sql=True)
    return buffer.getvalue()


def test_migrations_render_offline() -> None:
    sql = render_upgrade_sql()
    assert "CREATE EXTENSION IF NOT EXISTS timescaledb" in sql
    assert "add_retention_policy('offer_snapshots', INTERVAL '30 days')" in sql
    assert "add_compression_policy('offer_snapshots', INTERVAL '1 day')" in sql


def test_migrated_tables_exist_in_models() -> None:
    created = set(re.findall(r"CREATE TABLE (\w+)", render_upgrade_sql())) - {"alembic_version"}
    assert created <= set(Base.metadata.tables)


def test_all_models_are_migrated() -> None:
    created = set(re.findall(r"CREATE TABLE (\w+)", render_upgrade_sql())) - {"alembic_version"}
    assert created == set(Base.metadata.tables)


def test_hypertables_match_model_markers() -> None:
    sql = render_upgrade_sql()
    created = set(re.findall(r"CREATE TABLE (\w+)", sql))
    hypertables = set(re.findall(r"create_hypertable\('(\w+)'", sql))
    marked = {name for name, t in Base.metadata.tables.items() if "hypertable" in t.info}
    assert hypertables == marked & created


def test_seed_reference_data() -> None:
    sql = render_upgrade_sql()
    codes = ("'USDT'", "'UAH'", "'binance'", "'bybit'", "'okx'", "'monobank'", "'privatbank'")
    for code in (*codes, "'bitget'", "'mexc'", "'htx'"):
        assert code in sql
