"""Seed довідників: USDT, UAH, пара USDT/UAH, binance/bybit/okx, monobank/privatbank.

Мінімум, на який посилається collector. Решту активів, фіатів і способів оплати
додає оновлення довідника з ExchangeAdapter.list_markets().

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-04

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXCHANGES = [
    ("binance", "Binance P2P", "https://p2p.binance.com"),
    ("bybit", "Bybit P2P", "https://api2.bybit.com"),
    ("okx", "OKX P2P", "https://www.okx.com"),
]
PAYMENT_METHODS = [
    ("monobank", "Monobank"),
    ("privatbank", "PrivatBank"),
]


def upgrade() -> None:
    op.execute("INSERT INTO assets (code, name) VALUES ('USDT', 'Tether') ON CONFLICT DO NOTHING")
    op.execute(
        "INSERT INTO fiats (code, name) VALUES ('UAH', 'Українська гривня') ON CONFLICT DO NOTHING"
    )
    op.execute(
        "INSERT INTO currency_pairs (base_currency_id, quote_currency_id) "
        "SELECT a.id, f.id FROM assets a, fiats f WHERE a.code = 'USDT' AND f.code = 'UAH' "
        "ON CONFLICT DO NOTHING"
    )
    for code, name, api_url in EXCHANGES:
        op.execute(
            "INSERT INTO exchanges (code, name, api_url, status) "
            f"VALUES ('{code}', '{name}', '{api_url}', 'active') ON CONFLICT DO NOTHING"
        )
    for code, name in PAYMENT_METHODS:
        op.execute(
            "INSERT INTO payment_methods (code, name, fiat_id) "
            f"SELECT '{code}', '{name}', id FROM fiats WHERE code = 'UAH' ON CONFLICT DO NOTHING"
        )


def downgrade() -> None:
    codes = ", ".join(f"'{code}'" for code, _ in PAYMENT_METHODS)
    op.execute(f"DELETE FROM payment_methods WHERE code IN ({codes})")
    codes = ", ".join(f"'{code}'" for code, _, _ in EXCHANGES)
    op.execute(f"DELETE FROM exchanges WHERE code IN ({codes})")
    op.execute(
        "DELETE FROM currency_pairs WHERE base_currency_id = (SELECT id FROM assets WHERE code = "
        "'USDT') AND quote_currency_id = (SELECT id FROM fiats WHERE code = 'UAH')"
    )
    op.execute("DELETE FROM fiats WHERE code = 'UAH'")
    op.execute("DELETE FROM assets WHERE code = 'USDT'")
