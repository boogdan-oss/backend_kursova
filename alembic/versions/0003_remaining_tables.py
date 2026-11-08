"""Решта схеми: spread_history, opportunities, predictions (hypertables), сповіщення, спільнота.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

Money = sa.Numeric(28, 8)
Percent = sa.Numeric(12, 4)
TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "spread_history",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("time", TZ, nullable=False),
        sa.Column("pair_id", sa.Integer(), nullable=False),
        sa.Column("buy_exchange_id", sa.Integer(), nullable=False),
        sa.Column("sell_exchange_id", sa.Integer(), nullable=False),
        sa.Column("buy_price", Money, nullable=False),
        sa.Column("sell_price", Money, nullable=False),
        sa.Column("spread", Percent, nullable=False),
        sa.Column("net_spread", Percent, nullable=False),
        sa.PrimaryKeyConstraint("id", "time", name="pk_spread_history"),
        sa.ForeignKeyConstraint(
            ["pair_id"], ["currency_pairs.id"], name="fk_spread_history_pair_id_currency_pairs"
        ),
        sa.ForeignKeyConstraint(
            ["buy_exchange_id"],
            ["exchanges.id"],
            name="fk_spread_history_buy_exchange_id_exchanges",
        ),
        sa.ForeignKeyConstraint(
            ["sell_exchange_id"],
            ["exchanges.id"],
            name="fk_spread_history_sell_exchange_id_exchanges",
        ),
    )
    op.create_index(
        "ix_spread_history_pair_time", "spread_history", ["pair_id", sa.text("time DESC")]
    )

    op.create_table(
        "opportunities",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("time", TZ, nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("pair_id", sa.Integer(), nullable=False),
        sa.Column("buy_exchange_id", sa.Integer(), nullable=False),
        sa.Column("sell_exchange_id", sa.Integer(), nullable=False),
        sa.Column("buy_price", Money, nullable=False),
        sa.Column("sell_price", Money, nullable=False),
        sa.Column("steps", postgresql.JSONB(), nullable=False),
        sa.Column("spread", Percent, nullable=False),
        sa.Column("net_spread", Percent, nullable=False),
        sa.Column("estimated_profit", Money, nullable=False),
        sa.Column("max_volume", Money, nullable=False),
        sa.Column("rr", Percent, nullable=True),
        sa.Column("ai_score", sa.SmallInteger(), nullable=True),
        sa.Column("risk_score", sa.Float(), nullable=True),
        sa.Column("risk_level", sa.String(16), nullable=True),
        sa.Column("duration_sec", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", "time", name="pk_opportunities"),
        sa.CheckConstraint("ai_score BETWEEN 0 AND 100", name="ck_opportunities_ai_score_range"),
        sa.CheckConstraint("risk_score BETWEEN 0 AND 1", name="ck_opportunities_risk_score_range"),
        sa.CheckConstraint(
            "kind IN ('spread', 'cross_exchange', 'chain')", name="ck_opportunities_opportunitykind"
        ),
        sa.CheckConstraint(
            "risk_level IN ('low', 'medium', 'high')", name="ck_opportunities_risklevel"
        ),
        sa.ForeignKeyConstraint(
            ["pair_id"], ["currency_pairs.id"], name="fk_opportunities_pair_id_currency_pairs"
        ),
        sa.ForeignKeyConstraint(
            ["buy_exchange_id"], ["exchanges.id"], name="fk_opportunities_buy_exchange_id_exchanges"
        ),
        sa.ForeignKeyConstraint(
            ["sell_exchange_id"],
            ["exchanges.id"],
            name="fk_opportunities_sell_exchange_id_exchanges",
        ),
    )
    op.create_index(
        "ix_opportunities_pair_time", "opportunities", ["pair_id", sa.text("time DESC")]
    )

    op.create_table(
        "predictions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("time", TZ, nullable=False),
        sa.Column("model_version", sa.String(64), nullable=False),
        sa.Column("pair_id", sa.Integer(), nullable=False),
        sa.Column("exchange_id", sa.Integer(), nullable=True),
        sa.Column("target", sa.String(16), nullable=False),
        sa.Column("horizon", sa.SmallInteger(), nullable=False),
        sa.Column("predicted", Money, nullable=False),
        sa.Column("predicted_lower", Money, nullable=True),
        sa.Column("predicted_upper", Money, nullable=True),
        sa.Column("actual", Money, nullable=True),
        sa.PrimaryKeyConstraint("id", "time", name="pk_predictions"),
        sa.CheckConstraint(
            "target IN ('buy_price', 'sell_price', 'spread')",
            name="ck_predictions_predictiontarget",
        ),
        sa.ForeignKeyConstraint(
            ["pair_id"], ["currency_pairs.id"], name="fk_predictions_pair_id_currency_pairs"
        ),
        sa.ForeignKeyConstraint(
            ["exchange_id"], ["exchanges.id"], name="fk_predictions_exchange_id_exchanges"
        ),
    )
    op.create_index("ix_predictions_pair_time", "predictions", ["pair_id", sa.text("time DESC")])

    for table in ("spread_history", "opportunities", "predictions"):
        op.execute(
            f"SELECT create_hypertable('{table}', 'time', "
            "chunk_time_interval => INTERVAL '7 days', create_default_indexes => FALSE)"
        )

    op.create_table(
        "alert_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("pair_id", sa.Integer(), nullable=False),
        sa.Column("exchange_id", sa.Integer(), nullable=True),
        sa.Column("payment_method_id", sa.Integer(), nullable=True),
        sa.Column("min_spread", Percent, nullable=True),
        sa.Column("min_net_spread", Percent, nullable=True),
        sa.Column("min_ai_score", sa.SmallInteger(), nullable=True),
        sa.Column("min_rr", Percent, nullable=True),
        sa.Column("min_net_profit", Money, nullable=True),
        sa.Column("channels", postgresql.ARRAY(sa.String(16)), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_alert_rules"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_alert_rules_user_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["pair_id"],
            ["currency_pairs.id"],
            name="fk_alert_rules_pair_id_currency_pairs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["exchange_id"],
            ["exchanges.id"],
            name="fk_alert_rules_exchange_id_exchanges",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["payment_method_id"],
            ["payment_methods.id"],
            name="fk_alert_rules_payment_method_id_payment_methods",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_alert_rules_user_id", "alert_rules", ["user_id"])

    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("alert_rule_id", sa.Integer(), nullable=True),
        sa.Column("opportunity_id", sa.BigInteger(), nullable=True),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("sent_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("read", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_notifications"),
        sa.CheckConstraint(
            "channel IN ('in_app', 'telegram', 'email')",
            name="ck_notifications_notificationchannel",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_notifications_user_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["alert_rule_id"],
            ["alert_rules.id"],
            name="fk_notifications_alert_rule_id_alert_rules",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_notifications_user_id", "notifications", ["user_id"])

    op.create_table(
        "watchlist",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("pair_id", sa.Integer(), nullable=False),
        sa.Column("exchange_id", sa.Integer(), nullable=True),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_watchlist"),
        sa.UniqueConstraint(
            "user_id",
            "pair_id",
            "exchange_id",
            name="uq_watchlist_user_id_pair_id_exchange_id",
            postgresql_nulls_not_distinct=True,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_watchlist_user_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["pair_id"],
            ["currency_pairs.id"],
            name="fk_watchlist_pair_id_currency_pairs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["exchange_id"],
            ["exchanges.id"],
            name="fk_watchlist_exchange_id_exchanges",
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "posts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("attached_pair_id", sa.Integer(), nullable=True),
        sa.Column("attached_opportunity_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("updated_at", TZ, nullable=True),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_posts"),
        sa.CheckConstraint(
            "status IN ('visible', 'hidden', 'deleted')", name="ck_posts_contentstatus"
        ),
        sa.ForeignKeyConstraint(
            ["author_id"], ["users.id"], name="fk_posts_author_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["attached_pair_id"],
            ["currency_pairs.id"],
            name="fk_posts_attached_pair_id_currency_pairs",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_posts_author_id", "posts", ["author_id"])
    op.create_index("ix_posts_status_created_at", "posts", ["status", sa.text("created_at DESC")])

    op.create_table(
        "post_likes",
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("post_id", "user_id", name="pk_post_likes"),
        sa.ForeignKeyConstraint(
            ["post_id"], ["posts.id"], name="fk_post_likes_post_id_posts", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_post_likes_user_id_users", ondelete="CASCADE"
        ),
    )

    op.create_table(
        "comments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("updated_at", TZ, nullable=True),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_comments"),
        sa.CheckConstraint(
            "status IN ('visible', 'hidden', 'deleted')", name="ck_comments_contentstatus"
        ),
        sa.ForeignKeyConstraint(
            ["post_id"], ["posts.id"], name="fk_comments_post_id_posts", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["author_id"], ["users.id"], name="fk_comments_author_id_users", ondelete="CASCADE"
        ),
    )
    op.create_index("ix_comments_post_id", "comments", ["post_id"])

    op.create_table(
        "reposts",
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("post_id", "user_id", name="pk_reposts"),
        sa.ForeignKeyConstraint(
            ["post_id"], ["posts.id"], name="fk_reposts_post_id_posts", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_reposts_user_id_users", ondelete="CASCADE"
        ),
    )

    op.create_table(
        "reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=True),
        sa.Column("comment_id", sa.Integer(), nullable=True),
        sa.Column("reporter_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("resolved_by_id", sa.Integer(), nullable=True),
        sa.Column("resolved_at", TZ, nullable=True),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_reports"),
        sa.CheckConstraint(
            "(post_id IS NULL) <> (comment_id IS NULL)", name="ck_reports_single_target"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'resolved', 'rejected')", name="ck_reports_reportstatus"
        ),
        sa.ForeignKeyConstraint(
            ["post_id"], ["posts.id"], name="fk_reports_post_id_posts", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["comment_id"],
            ["comments.id"],
            name="fk_reports_comment_id_comments",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reporter_id"], ["users.id"], name="fk_reports_reporter_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by_id"],
            ["users.id"],
            name="fk_reports_resolved_by_id_users",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_reports_status", "reports", ["status"])

    op.create_table(
        "user_bans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("until", TZ, nullable=True),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("banned_by_id", sa.Integer(), nullable=True),
        sa.Column("lifted_at", TZ, nullable=True),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_user_bans"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_bans_user_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["banned_by_id"],
            ["users.id"],
            name="fk_user_bans_banned_by_id_users",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_user_bans_user_id", "user_bans", ["user_id"])


def downgrade() -> None:
    op.drop_table("user_bans")
    op.drop_table("reports")
    op.drop_table("reposts")
    op.drop_table("comments")
    op.drop_table("post_likes")
    op.drop_table("posts")
    op.drop_table("watchlist")
    op.drop_table("notifications")
    op.drop_table("alert_rules")
    op.drop_table("predictions")
    op.drop_table("opportunities")
    op.drop_table("spread_history")
