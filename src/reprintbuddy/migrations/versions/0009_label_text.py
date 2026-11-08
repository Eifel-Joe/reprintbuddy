"""article field "label text": free text for the label (Spec 5.2, 8.5)

NOTE for the merge of round v0.3.8: written while revision 0008 (part C) did not exist yet, so
`down_revision` is "0007" here and becomes "0008" when both parts are merged.

Revision ID: 0009
Revises: 0007
Create Date: 2026-11-08
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0007"  # becomes "0008" at the merge with part C
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("articles") as batch:
        batch.add_column(sa.Column("label_text", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("articles") as batch:
        batch.drop_column("label_text")
