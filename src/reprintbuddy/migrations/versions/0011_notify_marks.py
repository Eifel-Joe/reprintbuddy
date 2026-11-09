"""what the push messages have reported (Spec 10.6)

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-09

A new, empty table: nothing older is touched. Without push messages it stays empty.
"""
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notify_marks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("ref", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("streak", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("notified_at", sa.DateTime(), nullable=True),
        sa.Column("gone_since", sa.DateTime(), nullable=True),
        sa.CheckConstraint("state IN ('watch', 'due', 'sent')", name="ck_notify_marks_state"),
        sa.CheckConstraint("streak >= 0", name="ck_notify_marks_streak"),
        sa.UniqueConstraint("kind", "ref", name="uq_notify_marks_kind_ref"),
    )


def downgrade() -> None:
    op.drop_table("notify_marks")
