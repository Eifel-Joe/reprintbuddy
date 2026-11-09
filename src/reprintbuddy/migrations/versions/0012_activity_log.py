"""the activity log under Settings (Spec 10.7)

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-09

Two new, empty tables: nothing older is touched.
"""
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "activity",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("area", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("via", sa.String(), nullable=True),
        sa.Column("address", sa.String(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.CheckConstraint("actor IN ('user', 'auto', 'bambuddy', 'system')", name="ck_activity_actor"),
    )
    op.create_index("ix_activity_at", "activity", ["at"])
    op.create_index("ix_activity_area", "activity", ["area"])
    op.create_table(
        "activity_marks",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("activity_marks")
    op.drop_index("ix_activity_area", table_name="activity")
    op.drop_index("ix_activity_at", table_name="activity")
    op.drop_table("activity")
