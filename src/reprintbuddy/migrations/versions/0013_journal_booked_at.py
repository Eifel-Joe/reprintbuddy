"""journal: the booking date of each row (Spec 6.2, p172)

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-09

`booked_at` is when a booking counts (the date of a hand booking may lie in the past); `at`
stays the time it was saved. Every existing row was booked when it was saved, so it gets its `at`.

Added with a plain ALTER TABLE, not a batch rebuild: rebuilding `journal` writes its foreign keys
in an order that changes from run to run, and the restore of a backup compares the structure of
the database exactly (ops/fullbackup._structure), so a backup could be refused as "not a
Reprintbuddy database". SQLite adds a NOT NULL column only with a constant default; the constant is
never seen, every row gets its `at` right after, and the application always writes the column.
"""
import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "journal",
        sa.Column("booked_at", sa.DateTime(), nullable=False, server_default="1970-01-01 00:00:00.000000"),
    )
    op.execute("UPDATE journal SET booked_at = at")
    op.create_index("ix_journal_booked_at", "journal", ["booked_at"])


def downgrade() -> None:
    op.drop_index("ix_journal_booked_at", table_name="journal")
    with op.batch_alter_table("journal") as batch:
        batch.drop_column("booked_at")
