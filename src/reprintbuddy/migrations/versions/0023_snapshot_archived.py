"""the daily record keeps the last row of an archived spool (Spec 9.2, p717)

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-20

Until 1.2.1 the daily record dropped the row of a spool archived on the same day: a spool printed empty
and archived that day lost its use of the day. Now the spool keeps one row with its last weight, marked
`filament_snapshots.archived`; it counts for the use, not for the stock or the number of spools.
The step only adds the column (default false; every row of 1.2.1 is a living spool's), without
rebuilding the table, so the way back from the pre-release drops it again. Rejected: a table of its own
for archived spools (the use reads one series per spool, the mark keeps it in one place).
A row marked archived stays on the way back: in 1.2.1 it is an ordinary point of the spool's use, and
1.2.1 drops it itself at its next record if it is of that day.
Tests: tests/test_migration_0023_snapshot_archived.py, tests/test_migration_rules.py.
"""
import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

return_note = {
    "de": "Kennzeichen „archiviert“ an der täglichen Aufzeichnung der Spulen",
    "en": "Mark “archived” on the daily record of the spools",
}
rework = False


def upgrade() -> None:
    op.add_column("filament_snapshots", sa.Column("archived", sa.Boolean(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("filament_snapshots", "archived")
