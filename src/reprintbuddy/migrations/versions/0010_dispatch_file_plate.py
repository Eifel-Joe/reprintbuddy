"""each print keeps the file and plate it is handed over with (Spec 7.6)

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-08

Rows from before stay empty: which file they were really sent with is unknown, and an empty value
makes the search after an unclear answer use the article's file, as the versions before did.
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("dispatches") as batch:
        batch.add_column(sa.Column("library_file_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("plate_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("dispatches") as batch:
        batch.drop_column("plate_id")
        batch.drop_column("library_file_id")
