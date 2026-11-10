"""several suppliers per purchased part (Spec 5.3, 9.3, 9.4)

Revision ID: 0015
Revises: 0013
Create Date: 2026-11-10

Pack size, minimum order, lead time, supplier name and order link move from `parts` to the new
table `part_suppliers`; every existing part gets exactly one supplier, its default, with the values
it had (Spec 5.3: "Beim Update … sein einziger Lieferant und Standard"). The safety margin stays at
the part. A mark "ordered" keeps the pack size and the supplier name of the moment it was set
(Spec 9.4: the amount on its way does not follow a later change of the supplier), so
`reorder_marks` gets both, filled for the existing part marks from their part; and the supplier's
position, so a supplier without a name is still "Supplier n" on the list "on its way" (Spec 5.3).

`reorder_marks` gets its columns with a plain ALTER TABLE, not a batch rebuild (see 0013: a rebuild
may change the order of foreign keys, and the restore compares the structure exactly). `parts`
has to be rebuilt to lose its columns; it has no foreign keys of its own, and the keys of the
tables pointing at it name it, not its position, so an upgraded database has the structure of a
new one (tests/test_migration_0015_part_suppliers.py checks it, and a restore of a backup made
before this revision). The two CHECKs of the moved columns go with them.
"""
import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "part_suppliers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("part_id", sa.Integer(), sa.ForeignKey("parts.id"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("order_url", sa.String(), nullable=True),
        sa.Column("pack_size", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("min_order_packs", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("lead_time_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default="0"),
        sa.CheckConstraint("position >= 0", name="ck_part_suppliers_position"),
        sa.CheckConstraint("pack_size >= 1", name="ck_part_suppliers_pack_size"),
        sa.CheckConstraint("min_order_packs >= 1", name="ck_part_suppliers_min_order_packs"),
        sa.CheckConstraint("lead_time_days >= 0", name="ck_part_suppliers_lead_time_days"),
        sa.UniqueConstraint("part_id", "position", name="uq_part_suppliers_position"),
    )
    op.create_index(
        "uq_part_suppliers_default", "part_suppliers", ["part_id"], unique=True,
        sqlite_where=sa.text("is_default = 1"),
    )
    op.execute(
        "INSERT INTO part_suppliers"
        " (part_id, position, name, order_url, pack_size, min_order_packs, lead_time_days, is_default)"
        " SELECT id, 0, supplier, order_url, pack_size, min_order_packs, lead_time_days, 1 FROM parts"
    )

    op.add_column("reorder_marks", sa.Column("pack_size", sa.Integer(), nullable=True))
    op.add_column("reorder_marks", sa.Column("supplier_name", sa.String(), nullable=True))
    op.add_column("reorder_marks", sa.Column("supplier_position", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE reorder_marks SET"
        " pack_size = (SELECT pack_size FROM parts WHERE parts.id = reorder_marks.item_id),"
        " supplier_name = (SELECT supplier FROM parts WHERE parts.id = reorder_marks.item_id),"
        " supplier_position = 0"
        " WHERE item_type = 'part'"
    )

    with op.batch_alter_table("parts") as batch:
        batch.drop_constraint("ck_parts_pack_size", type_="check")
        batch.drop_constraint("ck_parts_min_order_packs", type_="check")
        for column in ("pack_size", "min_order_packs", "lead_time_days", "supplier", "order_url"):
            batch.drop_column(column)


def downgrade() -> None:
    """Back to one supplier per part: the default one; any other supplier is lost."""
    with op.batch_alter_table("parts") as batch:
        batch.add_column(sa.Column("pack_size", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("min_order_packs", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("lead_time_days", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("supplier", sa.String(), nullable=True))
        batch.add_column(sa.Column("order_url", sa.String(), nullable=True))
        batch.create_check_constraint("ck_parts_pack_size", "pack_size >= 1")
        batch.create_check_constraint("ck_parts_min_order_packs", "min_order_packs >= 1")
    for column in ("pack_size", "min_order_packs", "lead_time_days"):
        op.execute(
            f"UPDATE parts SET {column} = (SELECT s.{column} FROM part_suppliers s"
            " WHERE s.part_id = parts.id AND s.is_default = 1)"
            " WHERE EXISTS (SELECT 1 FROM part_suppliers s WHERE s.part_id = parts.id AND s.is_default = 1)"
        )
    for column, source in (("supplier", "name"), ("order_url", "order_url")):
        op.execute(
            f"UPDATE parts SET {column} = (SELECT s.{source} FROM part_suppliers s"
            " WHERE s.part_id = parts.id AND s.is_default = 1)"
        )
    with op.batch_alter_table("reorder_marks") as batch:
        batch.drop_column("supplier_position")
        batch.drop_column("supplier_name")
        batch.drop_column("pack_size")
    op.drop_index("uq_part_suppliers_default", table_name="part_suppliers")
    op.drop_table("part_suppliers")
