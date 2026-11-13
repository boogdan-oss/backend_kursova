"""Спільнота: деталі скарги й позначка модерації на публікаціях і коментарях.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-13

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MODERATED = ("posts", "comments")


def upgrade() -> None:
    op.add_column("reports", sa.Column("details", sa.String(1000), nullable=True))
    for table in MODERATED:
        op.add_column(table, sa.Column("moderated_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column(
            table,
            sa.Column(
                "moderated_by_id",
                sa.Integer(),
                sa.ForeignKey(
                    "users.id",
                    name=f"fk_{table}_moderated_by_id_users",
                    ondelete="SET NULL",
                ),
                nullable=True,
            ),
        )
    op.create_index("ix_reposts_user_id_created_at", "reposts", ["user_id", "created_at"])
    op.create_index("ix_post_likes_user_id_created_at", "post_likes", ["user_id", "created_at"])
    op.create_index("ix_reports_post_id", "reports", ["post_id"])
    op.create_index("ix_reports_comment_id", "reports", ["comment_id"])


def downgrade() -> None:
    op.drop_index("ix_reports_comment_id", "reports")
    op.drop_index("ix_reports_post_id", "reports")
    op.drop_index("ix_post_likes_user_id_created_at", "post_likes")
    op.drop_index("ix_reposts_user_id_created_at", "reposts")
    for table in MODERATED:
        op.drop_constraint(f"fk_{table}_moderated_by_id_users", table, type_="foreignkey")
        op.drop_column(table, "moderated_by_id")
        op.drop_column(table, "moderated_at")
    op.drop_column("reports", "details")
