"""sets in sets: a base row may hold another set (Spec 5.4, 0.4.3 p423)

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-12

A base row of a set (`set_positions.slot` 0) holds either an article, through a row in
`set_position_options` as before, or an inner set, through a row in the new table
`set_position_sets`: the inner set and its fixed variant, the chosen article per choice field of the
inner set (`choice1_id`, `choice2_id`, empty without choice parts). The variant is kept by its
articles, not by its number, so changing only a suffix of the inner set keeps it chosen.
Rejected: columns on `set_positions` (they would need a CHECK across columns and a rebuilt table to
go back down).

`order_lines.inner_path` keeps, for a line of an inner set, the way from the ordered set down to it
as it was when the order was placed: JSON `[[set_id, variant_sku, units], ...]`, empty for an article
directly in the ordered set (Spec 6.7). Sets and orders of earlier versions stay as they are.
Downgrade drops the base rows that hold an inner set together with the new table and column.
Tests: tests/test_migration_0020_sets_in_sets.py.
"""
import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "set_position_sets",
        sa.Column("position_id", sa.Integer(), sa.ForeignKey("set_positions.id"), primary_key=True),
        sa.Column("set_id", sa.Integer(), sa.ForeignKey("product_sets.id"), nullable=False),
        sa.Column("choice1_id", sa.Integer(), sa.ForeignKey("articles.id"), nullable=True),
        sa.Column("choice2_id", sa.Integer(), sa.ForeignKey("articles.id"), nullable=True),
    )
    op.add_column("order_lines", sa.Column("inner_path", sa.String(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    positions = [r[0] for r in conn.execute(sa.text("SELECT position_id FROM set_position_sets")).all()]
    op.drop_table("set_position_sets")
    for position_id in positions:
        conn.execute(sa.text("DELETE FROM set_positions WHERE id = :p"), {"p": position_id})
    op.execute("ALTER TABLE order_lines DROP COLUMN inner_path")
