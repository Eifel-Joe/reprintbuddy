"""catalog, stock, orders, journal and form tokens (B3)

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-05
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _int(name: str, default: int) -> sa.Column:
    return sa.Column(name, sa.Integer(), nullable=False, server_default=str(default))


def upgrade() -> None:
    op.create_table(
        "articles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("sku", sa.String(), nullable=True, unique=True),
        sa.Column("library_file_id", sa.Integer(), nullable=False),
        sa.Column("plate_id", sa.Integer(), nullable=False),
        sa.Column("file_name", sa.String(), nullable=False),
        sa.Column("file_fingerprint", sa.String(), nullable=True),
        _int("units_per_print", 1),
        _int("min_stock", 0),
        _int("buffer", 0),
        sa.Column("target_model", sa.String(), nullable=True),
        sa.Column("printer_id", sa.Integer(), nullable=True),
        sa.Column("needs_assembly", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("auto_enabled", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("pause_reason", sa.String(), nullable=True),
        _int("consecutive_failures", 0),
        _int("stock", 0),
        _int("reserved", 0),
        _int("provisional", 0),
        _int("awaiting_assembly", 0),
        sa.Column("thumbnail", sa.LargeBinary(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.CheckConstraint("stock >= 0", name="ck_articles_stock"),
        sa.CheckConstraint("reserved >= 0", name="ck_articles_reserved"),
        sa.CheckConstraint("provisional >= 0", name="ck_articles_provisional"),
        sa.CheckConstraint("awaiting_assembly >= 0", name="ck_articles_awaiting_assembly"),
        sa.CheckConstraint("units_per_print >= 1", name="ck_articles_units_per_print"),
    )
    op.create_index(
        "uq_articles_active_plate",
        "articles",
        ["library_file_id", "plate_id"],
        unique=True,
        sqlite_where=sa.text("archived_at IS NULL"),
    )

    op.create_table(
        "article_filaments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("article_id", sa.Integer(), sa.ForeignKey("articles.id"), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("grams", sa.Float(), nullable=False),
        sa.Column("material", sa.String(), nullable=True),
        sa.Column("color_hex", sa.String(), nullable=True),
        sa.Column("spool_filament_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "uq_article_filaments_slot", "article_filaments", ["article_id", "slot"], unique=True
    )

    op.create_table(
        "parts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("unit", sa.String(), nullable=False, server_default="pcs"),
        _int("stock", 0),
        _int("min_stock", 0),
        _int("pack_size", 1),
        _int("min_order_packs", 1),
        _int("lead_time_days", 0),
        _int("safety_value", 0),
        sa.Column("safety_unit", sa.String(), nullable=False, server_default="days"),
        sa.Column("supplier", sa.String(), nullable=True),
        sa.Column("order_url", sa.String(), nullable=True),
        sa.CheckConstraint("stock >= 0", name="ck_parts_stock"),
        sa.CheckConstraint("pack_size >= 1", name="ck_parts_pack_size"),
        sa.CheckConstraint("min_order_packs >= 1", name="ck_parts_min_order_packs"),
        sa.CheckConstraint("safety_unit IN ('days', 'qty')", name="ck_parts_safety_unit"),
    )

    op.create_table(
        "article_parts",
        sa.Column("article_id", sa.Integer(), sa.ForeignKey("articles.id"), primary_key=True),
        sa.Column("part_id", sa.Integer(), sa.ForeignKey("parts.id"), primary_key=True),
        sa.Column("qty_per_unit", sa.Integer(), nullable=False),
        sa.CheckConstraint("qty_per_unit >= 1", name="ck_article_parts_qty"),
    )

    op.create_table(
        "product_sets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("sku", sa.String(), nullable=True, unique=True),
    )

    op.create_table(
        "set_positions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("set_id", sa.Integer(), sa.ForeignKey("product_sets.id"), nullable=False),
        _int("sort", 0),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.CheckConstraint("qty >= 1", name="ck_set_positions_qty"),
    )

    op.create_table(
        "set_position_options",
        sa.Column(
            "position_id", sa.Integer(), sa.ForeignKey("set_positions.id"), primary_key=True
        ),
        sa.Column("article_id", sa.Integer(), sa.ForeignKey("articles.id"), primary_key=True),
    )

    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("reference", sa.String(), nullable=False),
        sa.Column("order_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="open"),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("status IN ('open', 'shipped', 'cancelled')", name="ck_orders_status"),
    )

    op.create_table(
        "order_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=False),
        _int("group_no", 1),
        sa.Column("article_id", sa.Integer(), sa.ForeignKey("articles.id"), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("set_id", sa.Integer(), sa.ForeignKey("product_sets.id"), nullable=True),
        sa.Column("set_qty", sa.Integer(), nullable=True),
        sa.CheckConstraint("qty >= 1", name="ck_order_lines_qty"),
    )

    op.create_table(
        "journal",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("article_id", sa.Integer(), sa.ForeignKey("articles.id"), nullable=True),
        sa.Column("part_id", sa.Integer(), sa.ForeignKey("parts.id"), nullable=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=True),
        _int("qty", 0),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.CheckConstraint("actor IN ('user', 'auto')", name="ck_journal_actor"),
    )
    op.create_index("ix_journal_at", "journal", ["at"])
    op.create_index("ix_journal_article_id", "journal", ["article_id"])

    op.create_table(
        "form_tokens",
        sa.Column("token", sa.String(), primary_key=True),
        sa.Column("used_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    for name in (
        "form_tokens",
        "journal",
        "order_lines",
        "orders",
        "set_position_options",
        "set_positions",
        "product_sets",
        "article_parts",
        "parts",
        "article_filaments",
        "articles",
    ):
        op.drop_table(name)
