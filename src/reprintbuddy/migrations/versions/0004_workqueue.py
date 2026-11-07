"""work queue, dispatches and inspections (B4)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-06
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

ORIGINS = "origin IN ('rule', 'scrap', 'failure', 'manual')"


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
    )


def upgrade() -> None:
    op.create_table(
        "work_queue",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("article_id", sa.Integer(), sa.ForeignKey("articles.id"), nullable=False),
        sa.Column("prints", sa.Integer(), nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("held", sa.Boolean(), nullable=False, server_default="0"),
        _created_at(),
        sa.CheckConstraint("prints >= 1", name="ck_work_queue_prints"),
        sa.CheckConstraint(ORIGINS, name="ck_work_queue_origin"),
    )
    op.create_index("ix_work_queue_position", "work_queue", ["position"])

    op.create_table(
        "dispatches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("article_id", sa.Integer(), sa.ForeignKey("articles.id"), nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("external_ref", sa.String(), nullable=False, unique=True),
        sa.Column("bambuddy_batch_id", sa.Integer(), nullable=True),
        sa.Column("bambuddy_item_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("units", sa.Integer(), nullable=False),
        sa.Column("enqueue_sent_at", sa.DateTime(), nullable=True),
        sa.Column("queue_entry_id", sa.Integer(), nullable=True),
        sa.Column("queue_position", sa.Integer(), nullable=False),
        sa.Column("last_status", sa.String(), nullable=True),
        sa.Column("message", sa.String(), nullable=True),
        _created_at(),
        sa.Column("dispatched_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(ORIGINS, name="ck_dispatches_origin"),
        sa.CheckConstraint(
            "status IN ('sending', 'unclear', 'queued', 'printing', 'completed', 'failed',"
            " 'cancelled', 'missing', 'returned')",
            name="ck_dispatches_status",
        ),
        sa.CheckConstraint("units >= 1", name="ck_dispatches_units"),
    )
    op.create_index("ix_dispatches_bambuddy_item_id", "dispatches", ["bambuddy_item_id"])
    op.create_index("ix_dispatches_status", "dispatches", ["status"])

    op.create_table(
        "inspections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "dispatch_id", sa.Integer(), sa.ForeignKey("dispatches.id"), nullable=False, unique=True
        ),
        sa.Column("article_id", sa.Integer(), sa.ForeignKey("articles.id"), nullable=False),
        sa.Column("units", sa.Integer(), nullable=False),
        sa.Column("good_units", sa.Integer(), nullable=True),
        _created_at(),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("units >= 1", name="ck_inspections_units"),
        sa.CheckConstraint(
            "good_units IS NULL OR good_units BETWEEN 0 AND units", name="ck_inspections_good_units"
        ),
    )


def downgrade() -> None:
    for name in ("inspections", "dispatches", "work_queue"):
        op.drop_table(name)
