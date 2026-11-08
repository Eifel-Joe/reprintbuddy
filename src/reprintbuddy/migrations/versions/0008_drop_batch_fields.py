"""drop the two fields of the old batch hand-over from dispatches (p56)

Since v0.3.5 every print goes to Bambuddy as a single queue entry (Spec 7.6). `external_ref` (NOT NULL
UNIQUE, only a random value since then) and `bambuddy_batch_id` were kept without a migration; nothing
reads either of them.

SQLite cannot drop a UNIQUE column, so the table is rebuilt the way SQLite documents it: a new table,
the rows copied, the old one dropped, the new one renamed, the indexes made again. Written out here and
not with Alembic's batch mode: the batch mode takes the CHECK constraints from a set, so their order in
the stored table definition changed from one run to the next, and a comparison of the schema text of
two equal databases (the restore of a full backup builds one) could differ for no reason.
`db.upgrade` runs this with foreign keys off (`inspections` points at `dispatches` and keeps doing so)
and checks them afterwards. Rows, ids, indexes and checks stay.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-08
"""
import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

ORIGINS = "origin IN ('rule', 'scrap', 'failure', 'manual')"
STATUSES = (
    "status IN ('sending', 'unclear', 'queued', 'printing', 'completed', 'failed',"
    " 'cancelled', 'missing', 'returned')"
)
KEPT = (
    "id, article_id, origin, bambuddy_item_id, status, units, enqueue_sent_at, queue_entry_id,"
    " queue_position, last_status, message, created_at, dispatched_at, finished_at"
)


def _create(name: str, *, with_old_fields: bool) -> None:
    columns = [
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("article_id", sa.Integer(), sa.ForeignKey("articles.id"), nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
    ]
    if with_old_fields:
        columns += [
            sa.Column("external_ref", sa.String(), nullable=False, unique=True),
            sa.Column("bambuddy_batch_id", sa.Integer(), nullable=True),
        ]
    columns += [
        sa.Column("bambuddy_item_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("units", sa.Integer(), nullable=False),
        sa.Column("enqueue_sent_at", sa.DateTime(), nullable=True),
        sa.Column("queue_entry_id", sa.Integer(), nullable=True),
        sa.Column("queue_position", sa.Integer(), nullable=False),
        sa.Column("last_status", sa.String(), nullable=True),
        sa.Column("message", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("dispatched_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(ORIGINS, name="ck_dispatches_origin"),
        sa.CheckConstraint(STATUSES, name="ck_dispatches_status"),
        sa.CheckConstraint("units >= 1", name="ck_dispatches_units"),
    ]
    op.create_table(name, *columns)


def _swap(new: str) -> None:
    op.drop_table("dispatches")  # its indexes go with it
    op.rename_table(new, "dispatches")
    op.create_index("ix_dispatches_bambuddy_item_id", "dispatches", ["bambuddy_item_id"])
    op.create_index("ix_dispatches_status", "dispatches", ["status"])


def upgrade() -> None:
    _create("dispatches_new", with_old_fields=False)
    op.execute(f"INSERT INTO dispatches_new ({KEPT}) SELECT {KEPT} FROM dispatches")
    _swap("dispatches_new")


def downgrade() -> None:
    # The fields come back empty: batch id none, `external_ref` a distinct random value per row
    # (it was NOT NULL UNIQUE), so that the previous version can insert prints again.
    _create("dispatches_old", with_old_fields=True)
    op.execute(
        f"INSERT INTO dispatches_old ({KEPT}, external_ref)"
        f" SELECT {KEPT}, lower(hex(randomblob(16))) FROM dispatches"
    )
    _swap("dispatches_old")
