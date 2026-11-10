"""several suppliers per spool sort, orders taken back (Spec 9.3, 9.4)

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-10

Lead time, roll size, minimum order, supplier name and order link move from `filament_settings`
to the new table `filament_suppliers`, built like `part_suppliers` (0015); every sort with settings
gets exactly one supplier, its default, with the values it had (Spec 9.3: "Beim Update … ihr
einziger Lieferant und Standard"). The safety margin stays at the settings. A filament mark now
keeps the roll size and the supplier of the moment it was set, as part marks do (Spec 9.4), so the
old filament marks get them from their settings; a sort without settings counted in the default
unit of 1000 g and has no supplier. `reorder_marks` learns `withdrawn` (Spec 9.4, p280: a mark
taken back is closed like an arrived one and carries the flag).

`reorder_marks` gets its column with a plain ALTER TABLE (see 0013, 0015). `filament_settings` has
to be rebuilt to lose its columns; it is written out, not done with `batch_alter_table`, whose
order of CHECKs is not stable (0015). Its one foreign key names its target, and the new table
pointing at it is created before the rebuild, so an upgraded database has the structure of a new
one (tests/test_migration_0016_filament_suppliers.py).
"""
import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

DEFAULT_ROLL_GRAMS = 1000  # what a filament mark without settings counted in (reorder.ordered_amount)


def upgrade() -> None:
    op.create_table(
        "filament_suppliers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "sort_id", sa.Integer(), sa.ForeignKey("filament_settings.spool_filament_id"), nullable=False
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("order_url", sa.String(), nullable=True),
        sa.Column("pack_size", sa.Integer(), nullable=False, server_default="1000"),
        sa.Column("min_order_packs", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("lead_time_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default="0"),
        sa.CheckConstraint("position >= 0", name="ck_filament_suppliers_position"),
        sa.CheckConstraint("pack_size >= 1", name="ck_filament_suppliers_pack_size"),
        sa.CheckConstraint("min_order_packs >= 1", name="ck_filament_suppliers_min_order_packs"),
        sa.CheckConstraint("lead_time_days >= 0", name="ck_filament_suppliers_lead_time_days"),
        sa.UniqueConstraint("sort_id", "position", name="uq_filament_suppliers_position"),
    )
    op.create_index(
        "uq_filament_suppliers_default", "filament_suppliers", ["sort_id"], unique=True,
        sqlite_where=sa.text("is_default = 1"),
    )
    op.execute(
        "INSERT INTO filament_suppliers"
        " (sort_id, position, name, order_url, pack_size, min_order_packs, lead_time_days, is_default)"
        " SELECT spool_filament_id, 0, supplier, order_url, pack_grams, min_order_packs, lead_time_days, 1"
        " FROM filament_settings"
    )

    op.add_column(
        "reorder_marks", sa.Column("withdrawn", sa.Boolean(), nullable=False, server_default="0")
    )
    op.execute(
        "UPDATE reorder_marks SET"
        f" pack_size = COALESCE((SELECT pack_grams FROM filament_settings s"
        f" WHERE s.spool_filament_id = reorder_marks.item_id), {DEFAULT_ROLL_GRAMS}),"
        " supplier_name = (SELECT supplier FROM filament_settings s WHERE s.spool_filament_id = reorder_marks.item_id),"
        " supplier_position = (SELECT 0 FROM filament_settings s WHERE s.spool_filament_id = reorder_marks.item_id)"
        " WHERE item_type = 'filament'"
    )

    # Written out like the rebuild of `parts` in 0015 (stable SQL text for the restore's comparison).
    op.execute(SETTINGS_TABLE)
    op.execute(
        f"INSERT INTO _alembic_tmp_filament_settings ({SETTINGS_COLUMNS})"
        f" SELECT {SETTINGS_COLUMNS} FROM filament_settings"
    )
    op.execute("DROP TABLE filament_settings")
    op.execute("ALTER TABLE _alembic_tmp_filament_settings RENAME TO filament_settings")


SETTINGS_COLUMNS = "spool_filament_id, safety_value, safety_unit"
# `filament_settings` without the moved columns, in the form SQLAlchemy writes it (models.FilamentSettings).
SETTINGS_TABLE = """CREATE TABLE _alembic_tmp_filament_settings (
	spool_filament_id INTEGER NOT NULL,
	safety_value INTEGER DEFAULT '0' NOT NULL,
	safety_unit VARCHAR DEFAULT 'days' NOT NULL,
	PRIMARY KEY (spool_filament_id),
	CONSTRAINT ck_filament_settings_unit CHECK (safety_unit IN ('days', 'grams')),
	FOREIGN KEY(spool_filament_id) REFERENCES filament_sorts (id)
)"""


def downgrade() -> None:
    """Back to one supplier per sort: the default one; any other supplier is lost. A mark taken
    back stays closed (0015 knows only arrived); filament marks count with the settings again."""
    with op.batch_alter_table("filament_settings") as batch:
        batch.add_column(sa.Column("lead_time_days", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("pack_grams", sa.Integer(), nullable=False, server_default="1000"))
        batch.add_column(sa.Column("min_order_packs", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("supplier", sa.String(), nullable=True))
        batch.add_column(sa.Column("order_url", sa.String(), nullable=True))
        batch.create_check_constraint("ck_filament_settings_pack_grams", "pack_grams >= 1")
        batch.create_check_constraint("ck_filament_settings_min_order_packs", "min_order_packs >= 1")
    default = (
        "FROM filament_suppliers f WHERE f.sort_id = filament_settings.spool_filament_id AND f.is_default = 1"
    )
    for column, source in (
        ("lead_time_days", "lead_time_days"), ("pack_grams", "pack_size"), ("min_order_packs", "min_order_packs"),
    ):
        op.execute(
            f"UPDATE filament_settings SET {column} = (SELECT f.{source} {default}) WHERE EXISTS (SELECT 1 {default})"
        )
    for column, source in (("supplier", "name"), ("order_url", "order_url")):
        op.execute(f"UPDATE filament_settings SET {column} = (SELECT f.{source} {default})")
    op.execute(
        "UPDATE reorder_marks SET pack_size = NULL, supplier_name = NULL, supplier_position = NULL"
        " WHERE item_type = 'filament'"
    )
    with op.batch_alter_table("reorder_marks") as batch:
        batch.drop_column("withdrawn")
    op.drop_index("uq_filament_suppliers_default", table_name="filament_suppliers")
    op.drop_table("filament_suppliers")
