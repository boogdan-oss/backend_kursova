"""Біржі OKX, Bitget, MEXC, HTX і стиснення offer_snapshots через 1 день замість 7.

Стиснення раніше: дані колектора обмежені бюджетом місця, а стиснуті чанки займають
приблизно вдесятеро менше, тож у той самий бюджет вміщається довша історія.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-08

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXCHANGES = [
    ("okx", "OKX P2P", "https://www.okx.com"),
    ("bitget", "Bitget P2P", "https://www.bitget.com"),
    ("mexc", "MEXC P2P", "https://p2p.mexc.com"),
    ("htx", "HTX P2P", "https://www.htx.com"),
]
NEW_EXCHANGES = ("bitget", "mexc", "htx")


def _compress_after(interval: str) -> None:
    op.execute("SELECT remove_compression_policy('offer_snapshots', if_exists => TRUE)")
    op.execute(f"SELECT add_compression_policy('offer_snapshots', INTERVAL '{interval}')")


def upgrade() -> None:
    for code, name, api_url in EXCHANGES:
        op.execute(
            "INSERT INTO exchanges (code, name, api_url, status) "
            f"VALUES ('{code}', '{name}', '{api_url}', 'active') ON CONFLICT DO NOTHING"
        )
    _compress_after("1 day")


def downgrade() -> None:
    _compress_after("7 days")
    codes = ", ".join(f"'{code}'" for code in NEW_EXCHANGES)
    op.execute(f"DELETE FROM exchanges WHERE code IN ({codes})")
