"""Вхід і сповіщення: refresh-токени, назва й спосіб оплати в правилах, одне сповіщення на подію.

Сповіщення тепер одне на спрацювання правила: канали — масивом, результат доставки
кожного каналу — у delivery, текст і можливість — у text і data. Таблиця notifications
до цього не використовувалась, тому стара колонка channel просто видаляється.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-12

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("expires_at", TZ, nullable=False),
        sa.Column("revoked_at", TZ, nullable=True),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_refresh_tokens"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_refresh_tokens_user_id_users", ondelete="CASCADE"
        ),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])

    op.drop_constraint(
        "fk_alert_rules_payment_method_id_payment_methods", "alert_rules", type_="foreignkey"
    )
    op.drop_column("alert_rules", "payment_method_id")
    op.add_column("alert_rules", sa.Column("payment", sa.String(64), nullable=True))
    op.add_column("alert_rules", sa.Column("title", sa.String(100), nullable=True))

    op.drop_constraint("ck_notifications_notificationchannel", "notifications", type_="check")
    op.drop_column("notifications", "channel")
    op.add_column(
        "notifications",
        sa.Column(
            "channels",
            postgresql.ARRAY(sa.String(16)),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    op.add_column(
        "notifications",
        sa.Column("delivery", postgresql.JSONB(), server_default=sa.text("'{}'"), nullable=False),
    )
    op.add_column("notifications", sa.Column("text", sa.Text(), server_default="", nullable=False))
    op.add_column(
        "notifications",
        sa.Column("data", postgresql.JSONB(), server_default=sa.text("'{}'"), nullable=False),
    )
    op.create_index(
        "ix_notifications_user_sent", "notifications", ["user_id", sa.text("sent_at DESC")]
    )
    op.create_index("ix_notifications_alert_rule_id", "notifications", ["alert_rule_id"])


def downgrade() -> None:
    op.drop_index("ix_notifications_alert_rule_id", "notifications")
    op.drop_index("ix_notifications_user_sent", "notifications")
    op.drop_column("notifications", "data")
    op.drop_column("notifications", "text")
    op.drop_column("notifications", "delivery")
    op.drop_column("notifications", "channels")
    op.add_column(
        "notifications",
        sa.Column("channel", sa.String(16), server_default="in_app", nullable=False),
    )
    op.create_check_constraint(
        "ck_notifications_notificationchannel",
        "notifications",
        "channel IN ('in_app', 'telegram', 'email')",
    )

    op.drop_column("alert_rules", "title")
    op.drop_column("alert_rules", "payment")
    op.add_column("alert_rules", sa.Column("payment_method_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_alert_rules_payment_method_id_payment_methods",
        "alert_rules",
        "payment_methods",
        ["payment_method_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_index("ix_refresh_tokens_user_id", "refresh_tokens")
    op.drop_table("refresh_tokens")
