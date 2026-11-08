"""article field "label text": free text for the label (Spec 5.2, 8.5)

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-08
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("articles") as batch:
        batch.add_column(sa.Column("label_text", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("articles") as batch:
        batch.drop_column("label_text")
