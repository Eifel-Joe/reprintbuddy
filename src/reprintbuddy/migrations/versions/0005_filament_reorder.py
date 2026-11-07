"""filament sorts, settings, daily spool records and reorder marks (B6)

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-06
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "filament_sorts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sort_key", sa.String(), nullable=False, unique=True),
        sa.Column("brand", sa.String(), nullable=True),
        sa.Column("material", sa.String(), nullable=False),
        sa.Column("subtype", sa.String(), nullable=True),
        sa.Column("color_hex", sa.String(), nullable=True),
        sa.Column("color_name", sa.String(), nullable=True),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column(
            "first_seen_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
    )

    op.create_table(
        "filament_settings",
        sa.Column(
            "spool_filament_id", sa.Integer(), sa.ForeignKey("filament_sorts.id"), primary_key=True
        ),
        sa.Column("lead_time_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("safety_value", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("safety_unit", sa.String(), nullable=False, server_default="days"),
        sa.Column("pack_grams", sa.Integer(), nullable=False, server_default="1000"),
        sa.Column("min_order_packs", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("supplier", sa.String(), nullable=True),
        sa.Column("order_url", sa.String(), nullable=True),
        sa.CheckConstraint("safety_unit IN ('days', 'grams')", name="ck_filament_settings_unit"),
        sa.CheckConstraint("pack_grams >= 1", name="ck_filament_settings_pack_grams"),
        sa.CheckConstraint("min_order_packs >= 1", name="ck_filament_settings_min_order_packs"),
    )

    op.create_table(
        "filament_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("spool_id", sa.Integer(), nullable=False),
        sa.Column(
            "spool_filament_id", sa.Integer(), sa.ForeignKey("filament_sorts.id"), nullable=False
        ),
        sa.Column("remaining_g", sa.Float(), nullable=False),
        sa.Column("label_g", sa.Integer(), nullable=False),
        sa.Column("spool_created_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "uq_filament_snapshots_day_spool", "filament_snapshots", ["day", "spool_id"], unique=True
    )
    op.create_index(
        "ix_filament_snapshots_sort_day", "filament_snapshots", ["spool_filament_id", "day"]
    )

    op.create_table(
        "reorder_marks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("item_type", sa.String(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column(
            "marked_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column("received_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("item_type IN ('filament', 'part')", name="ck_reorder_marks_item_type"),
        sa.CheckConstraint("qty >= 1", name="ck_reorder_marks_qty"),
    )
    op.create_index("ix_reorder_marks_item", "reorder_marks", ["item_type", "item_id"])


def downgrade() -> None:
    for name in ("reorder_marks", "filament_snapshots", "filament_settings", "filament_sorts"):
        op.drop_table(name)
