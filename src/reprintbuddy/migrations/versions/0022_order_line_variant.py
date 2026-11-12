"""an order line of an article made of parts keeps its variant (Spec 5.6, 0.4.5 p360)

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-12

A line of an article made of parts built on order kept only the number of its variant as ordered
(`order_lines.variant_sku`). The check "a variant in an open order cannot go" (Spec 5.6) compared that
number with the number the variant has now: after its suffix or the base number changed, the variant
could be dropped although an open order held it. `order_lines.variant_id` names the variant row
itself (an article without choices: the article), which keeps its id when its number changes.
Rejected: refusing to drop any variant while an open order holds a number no variant has (it would
also block variants the order does not hold).

The update fills the column for the lines of such articles where the number as ordered is still the
number of a variant row of that article; an article without choices is its only variant. A line whose
variant was renamed before the update stays empty and keeps the check by number, as before. The
number as ordered and every other column stay as they are. Downgrade drops the column.
Tests: tests/test_migration_0022_order_variant.py.
"""
import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE order_lines ADD COLUMN variant_id INTEGER REFERENCES articles (id)")
    conn = op.get_bind()
    # the variant row of the article whose number is the number as ordered
    conn.execute(sa.text(
        "UPDATE order_lines SET variant_id = ("
        " SELECT v.id FROM articles v WHERE v.kind = 'variant' AND v.parent_id = order_lines.assembled_id"
        " AND v.sku = order_lines.variant_sku)"
        " WHERE assembled_id IS NOT NULL AND variant_sku IS NOT NULL"
    ))
    # an article without choices: it is its own variant (no variant row of it is active)
    conn.execute(sa.text(
        "UPDATE order_lines SET variant_id = assembled_id"
        " WHERE assembled_id IS NOT NULL AND variant_id IS NULL"
        " AND NOT EXISTS (SELECT 1 FROM articles v WHERE v.kind = 'variant' AND v.parent_id = order_lines.assembled_id"
        " AND v.archived_at IS NULL)"
        " AND (variant_sku IS NULL OR variant_sku = (SELECT a.sku FROM articles a WHERE a.id = order_lines.assembled_id))"
    ))


def downgrade() -> None:
    with op.batch_alter_table("order_lines") as batch:
        batch.drop_column("variant_id")
