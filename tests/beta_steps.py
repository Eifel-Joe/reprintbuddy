"""Test migration steps after the head of the last stable version (Spec 11.3a "Rückweg zur stabilen Version").

`beta_scripts(tmp_path, "add_table", "add_column")` copies this version's migration folder and appends the
named steps after its head, in order, as revisions t01, t02, …. The steps are what a pre-release could bring:
steps that only add (case 2 of the way back), a step marked as rework (case 3), and steps that break the
rule "only add" without saying so, which the check in tests/migration_rules.py must catch.
Tests: tests/test_beta_steps.py, tests/test_migration_rules.py, tests/ops/test_stablereturn.py.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from reprintbuddy.db import MIGRATIONS_DIR
from reprintbuddy.ops.migrationsteps import head_of_dir

_HEADER = '''import sqlalchemy as sa
from alembic import op

revision = "@REV@"
down_revision = "@DOWN@"
branch_labels = None
depends_on = None
'''

_NOTE = 'return_note = {"de": "@DE@", "en": "@EN@"}\nrework = False\n'


def _only_adds(de: str, en: str, body: str) -> str:
    return _HEADER + _NOTE.replace("@DE@", de).replace("@EN@", en) + body


STEPS: dict[str, str] = {
    # --- only adds: case 2 ---------------------------------------------------------------------------
    "add_table": _only_adds("Testnotizen", "Test notes", '''
def upgrade():
    op.create_table(
        "t_notes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("order_id", sa.Integer, sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("text", sa.String, nullable=False),
    )
    op.create_index("ix_t_notes_order_id", "t_notes", ["order_id"])


def downgrade():
    op.drop_index("ix_t_notes_order_id", table_name="t_notes")
    op.drop_table("t_notes")
'''),
    "add_column": _only_adds("Testmarken an Bestellungen", "Test marks on orders", '''
def upgrade():
    op.add_column("orders", sa.Column("t_flag", sa.Integer, nullable=True))
    op.create_index("ix_orders_t_flag", "orders", ["t_flag"])


def downgrade():
    op.drop_index("ix_orders_t_flag", table_name="orders")
    op.drop_column("orders", "t_flag")
'''),
    # --- marked as rework: case 3 ----------------------------------------------------------------------
    "rework_marked": _HEADER + '''return_note = {"de": "Bestellnummern in Großbuchstaben", "en": "Order references in capitals"}
rework = True


def upgrade():
    op.execute("UPDATE orders SET reference = upper(reference)")


def downgrade():
    raise NotImplementedError("the old references are gone")
''',
    # --- break the rule without saying so: the check must catch each -----------------------------------
    "index_existing": _only_adds("Index", "Index", '''
def upgrade():
    op.create_index("ix_t_orders_reference", "orders", ["reference"])


def downgrade():
    op.drop_index("ix_t_orders_reference", table_name="orders")
'''),
    "unique_existing": _only_adds("Eindeutig", "Unique", '''
def upgrade():
    op.create_index("ux_t_orders_id_reference", "orders", ["id", "reference"], unique=True)


def downgrade():
    op.drop_index("ux_t_orders_id_reference", table_name="orders")
'''),
    "trigger_existing": _only_adds("Trigger", "Trigger", '''
def upgrade():
    op.execute("CREATE TRIGGER t_orders_insert AFTER INSERT ON orders BEGIN SELECT 1; END")


def downgrade():
    op.execute("DROP TRIGGER t_orders_insert")
'''),
    "rebuild": _only_adds("Neubau", "Rebuild", '''
def upgrade():
    with op.batch_alter_table("orders", recreate="always") as batch:
        batch.add_column(sa.Column("t_rebuilt", sa.Integer, nullable=True))


def downgrade():
    with op.batch_alter_table("orders", recreate="always") as batch:
        batch.drop_column("t_rebuilt")
'''),
    "rewrite_rows": _only_adds("Umschreiben", "Rewrite", '''
def upgrade():
    op.execute("UPDATE orders SET reference = reference || ' x'")


def downgrade():
    op.execute("UPDATE orders SET reference = substr(reference, 1, length(reference) - 2)")
'''),
    "rename": _only_adds("Umbenennen", "Rename", '''
def upgrade():
    op.execute("ALTER TABLE orders RENAME COLUMN note TO t_note")


def downgrade():
    op.execute("ALTER TABLE orders RENAME COLUMN t_note TO note")
'''),
    "bad_downgrade": _only_adds("Bleibt liegen", "Left behind", '''
def upgrade():
    op.create_table("t_left", sa.Column("id", sa.Integer, primary_key=True))


def downgrade():
    pass
'''),
    "raising_downgrade": _only_adds("Rückbau scheitert", "Downgrade fails", '''
def upgrade():
    op.create_table("t_fail", sa.Column("id", sa.Integer, primary_key=True))


def downgrade():
    raise RuntimeError("this step cannot go back")
'''),
    "no_note": _HEADER + '''

def upgrade():
    op.create_table("t_plain", sa.Column("id", sa.Integer, primary_key=True))


def downgrade():
    op.drop_table("t_plain")
''',
}

# The steps that break the rule "only add" while marked as adding only (tests/test_migration_rules.py).
VIOLATIONS = ("index_existing", "unique_existing", "trigger_existing", "rebuild", "rewrite_rows", "rename", "bad_downgrade")


def revision_of(position: int) -> str:
    """The revision of the n-th appended step (1-based): t01, t02, …"""
    return f"t{position:02d}"


def beta_scripts(tmp_path: Path, *steps: str) -> Path:
    """A copy of this version's migration folder with `steps` appended after its head; the script location."""
    target = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, target, ignore=shutil.ignore_patterns("__pycache__"))
    down = head_of_dir(target / "versions")
    for position, name in enumerate(steps, start=1):
        revision = revision_of(position)
        text = STEPS[name].replace("@REV@", revision).replace("@DOWN@", down)
        (target / "versions" / f"{revision}_{name}.py").write_text(text, encoding="utf-8")
        down = revision
    return target
