"""spool sorts with the brand and a group of spool sorts per slot (Spec 4, 5.2, 9.1)

A spool sort is brand + material + subtype + colour value again; which spool sorts an article may
use is ticked per slot of its print file (`article_filament_sorts`), one of them "is reordered".
The brandless sorts of 0006 cannot be split here: the brands only come with Bambuddy's spool list.
So every existing sort becomes a legacy sort (its key can never meet a new one), and all slot
mappings are dropped (the user ticks again per article). Settings, open "ordered" marks and daily
rows stay on the legacy sorts; `filament.convert_legacy` moves them on the first spool list.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-07
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("filament_sorts") as batch:
        batch.add_column(sa.Column("legacy", sa.Boolean(), nullable=False, server_default="0"))
    op.execute("UPDATE filament_sorts SET legacy = 1, sort_key = 'legacy:' || id")

    with op.batch_alter_table("article_filaments") as batch:
        batch.drop_column("spool_filament_id")

    op.create_table(
        "article_filament_sorts",
        sa.Column("article_id", sa.Integer(), sa.ForeignKey("articles.id"), primary_key=True),
        sa.Column("slot", sa.Integer(), primary_key=True),
        sa.Column("sort_id", sa.Integer(), sa.ForeignKey("filament_sorts.id"), primary_key=True),
        sa.Column("reordered", sa.Boolean(), nullable=False, server_default="0"),
    )
    op.create_index(
        "uq_article_filament_sorts_reordered",
        "article_filament_sorts",
        ["article_id", "slot"],
        unique=True,
        sqlite_where=sa.text("reordered = 1"),
    )
    op.create_index("ix_article_filament_sorts_sort", "article_filament_sorts", ["sort_id"])


def downgrade() -> None:
    pass  # the old mappings are gone; a legacy sort cannot be told apart again
