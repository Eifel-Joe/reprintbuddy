"""prints withdrawn after a cancel (Spec 7.2a, 0.4.1 p382)

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-11

A handed-over print can end as `withdrawn`: after a cancel Reprintbuddy deleted it, still waiting,
from Bambuddy's queue. `withdraw_sent_at` marks a print whose delete was sent; the poll then never
takes its disappearance for a delete by somebody else (that pauses the article, Spec 7.6). The new
table `withdrawals` keeps per article how many prints still have to come out of Bambuddy, for which
order, and Bambuddy's refusal if it refused.

`dispatches` gets a new state in its CHECK, so it is rebuilt, written out like `articles` in 0018
and not with `batch_alter_table`, whose order of CHECKs is not stable (0015). Its columns keep their
order (`library_file_id`, `plate_id` stay where 0010 put them); the new column follows.
`inspections.dispatch_id` points at `dispatches` by name, so after the rename it points at the new
table; the migration runs with foreign keys off and checks them after (db.py), as 0018 does for
`articles`. Tests: tests/test_migration_0019_withdrawals.py.
"""
import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

COLUMNS = (
    "id, article_id, origin, bambuddy_item_id, status, units, enqueue_sent_at, queue_entry_id,"
    " queue_position, last_status, message, created_at, dispatched_at, finished_at, library_file_id, plate_id"
)

_TABLE = """CREATE TABLE _alembic_tmp_dispatches (
	id INTEGER NOT NULL,
	article_id INTEGER NOT NULL,
	origin VARCHAR NOT NULL,
	bambuddy_item_id INTEGER,
	status VARCHAR NOT NULL,
	units INTEGER NOT NULL,
	enqueue_sent_at DATETIME,
	queue_entry_id INTEGER,
	queue_position INTEGER NOT NULL,
	last_status VARCHAR,
	message VARCHAR,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	dispatched_at DATETIME,
	finished_at DATETIME, library_file_id INTEGER, plate_id INTEGER{extra},
	PRIMARY KEY (id),
	CONSTRAINT ck_dispatches_origin CHECK (origin IN ('rule', 'scrap', 'failure', 'manual')),
	CONSTRAINT ck_dispatches_status CHECK (status IN ({states})),
	CONSTRAINT ck_dispatches_units CHECK (units >= 1),
	FOREIGN KEY(article_id) REFERENCES articles (id)
)"""

OLD_STATES = "'sending', 'unclear', 'queued', 'printing', 'completed', 'failed', 'cancelled', 'missing', 'returned'"
NEW_TABLE = _TABLE.format(extra=", withdraw_sent_at DATETIME", states=OLD_STATES + ", 'withdrawn'")
OLD_TABLE = _TABLE.format(extra="", states=OLD_STATES)


def _rebuild(table: str) -> None:
    op.execute(table)
    op.execute(f"INSERT INTO _alembic_tmp_dispatches ({COLUMNS}) SELECT {COLUMNS} FROM dispatches")
    op.execute("DROP TABLE dispatches")
    op.execute("ALTER TABLE _alembic_tmp_dispatches RENAME TO dispatches")
    op.create_index("ix_dispatches_bambuddy_item_id", "dispatches", ["bambuddy_item_id"])
    op.create_index("ix_dispatches_status", "dispatches", ["status"])


def upgrade() -> None:
    _rebuild(NEW_TABLE)
    op.create_table(
        "withdrawals",
        sa.Column("article_id", sa.Integer(), sa.ForeignKey("articles.id"), primary_key=True),
        sa.Column("prints", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=True),
        sa.Column("refused", sa.String(), nullable=True),
        sa.Column("unreached", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("prints >= 0", name="ck_withdrawals_prints"),
    )


def downgrade() -> None:
    """A withdrawn print becomes `missing`, the nearest older state: it is gone from Bambuddy and
    no longer counts "in progress". Its pause question does not arise, it is finished."""
    op.drop_table("withdrawals")
    op.execute("UPDATE dispatches SET status = 'missing' WHERE status = 'withdrawn'")
    _rebuild(OLD_TABLE)
