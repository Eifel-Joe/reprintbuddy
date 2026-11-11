"""articles made of parts and their variants (Spec 5.6, 0.4.0 p341)

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-11

An article is now a print article (its own plate, as before), an article made of parts (a bill of
materials of print articles, built on stock or on order), or one variant of such an article with
choices, which is an article row of its own under its parent so stock, journal, counts and order
lines work for it unchanged. Every article before this update is a print article and sold alone
("einzeln erhaeltlich", Spec 5.2, default on).

`articles` loses NOT NULL on the plate columns (only a print article has a plate, a CHECK says so),
so it is rebuilt. Written out like `filament_settings` in 0016, not with `batch_alter_table`, whose
order of CHECKs is not stable (0015): the columns keep their order, `label_text` stays where 0009
put it, and the new columns follow. Other tables point at `articles` by name, so after the rename
they point at the new table; the migration runs with foreign keys off and checks them after (db.py).
The bill of materials gets two tables, `article_options` (an option of choice field 1 or 2 with its
suffix) and `article_components` (a print article and its quantity, as base part or in an option);
`order_lines.assembled_id` names the article made of parts a group of part lines stands for, added
with a plain ALTER TABLE (see 0013). Tests: tests/test_migration_0018_assembled.py.
"""
import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

OLD_COLUMNS = (
    "id, name, archived_at, sku, library_file_id, plate_id, file_name, file_fingerprint, units_per_print,"
    " min_stock, buffer, target_model, printer_id, needs_assembly, auto_enabled, paused, pause_reason,"
    " consecutive_failures, stock, reserved, provisional, awaiting_assembly, thumbnail, created_at, label_text"
)

# `articles` with optional plate columns and the new ones, in the form SQLAlchemy writes it.
NEW_TABLE = """CREATE TABLE _alembic_tmp_articles (
	id INTEGER NOT NULL,
	name VARCHAR NOT NULL,
	archived_at DATETIME,
	sku VARCHAR,
	library_file_id INTEGER,
	plate_id INTEGER,
	file_name VARCHAR,
	file_fingerprint VARCHAR,
	units_per_print INTEGER DEFAULT '1' NOT NULL,
	min_stock INTEGER DEFAULT '0' NOT NULL,
	buffer INTEGER DEFAULT '0' NOT NULL,
	target_model VARCHAR,
	printer_id INTEGER,
	needs_assembly BOOLEAN DEFAULT '0' NOT NULL,
	auto_enabled BOOLEAN DEFAULT '0' NOT NULL,
	paused BOOLEAN DEFAULT '0' NOT NULL,
	pause_reason VARCHAR,
	consecutive_failures INTEGER DEFAULT '0' NOT NULL,
	stock INTEGER DEFAULT '0' NOT NULL,
	reserved INTEGER DEFAULT '0' NOT NULL,
	provisional INTEGER DEFAULT '0' NOT NULL,
	awaiting_assembly INTEGER DEFAULT '0' NOT NULL,
	thumbnail BLOB,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	label_text VARCHAR,
	kind VARCHAR DEFAULT 'print' NOT NULL,
	sold_alone BOOLEAN DEFAULT '1' NOT NULL,
	assembly_mode VARCHAR,
	parent_id INTEGER,
	variant_key VARCHAR,
	PRIMARY KEY (id),
	CONSTRAINT ck_articles_stock CHECK (stock >= 0),
	CONSTRAINT ck_articles_reserved CHECK (reserved >= 0),
	CONSTRAINT ck_articles_provisional CHECK (provisional >= 0),
	CONSTRAINT ck_articles_awaiting_assembly CHECK (awaiting_assembly >= 0),
	CONSTRAINT ck_articles_units_per_print CHECK (units_per_print >= 1),
	CONSTRAINT ck_articles_kind CHECK (kind IN ('print', 'assembled', 'variant')),
	CONSTRAINT ck_articles_print_has_plate CHECK (kind <> 'print' OR (library_file_id IS NOT NULL AND plate_id IS NOT NULL AND file_name IS NOT NULL)),
	CONSTRAINT ck_articles_variant_parent CHECK ((kind = 'variant') = (parent_id IS NOT NULL AND variant_key IS NOT NULL)),
	CONSTRAINT ck_articles_assembly_mode CHECK ((kind = 'assembled') = (COALESCE(assembly_mode, '') IN ('stock', 'order'))),
	UNIQUE (sku),
	FOREIGN KEY(parent_id) REFERENCES articles (id)
)"""

# `articles` as revision 0017 left it, for the downgrade.
OLD_TABLE = """CREATE TABLE _alembic_tmp_articles (
	id INTEGER NOT NULL,
	name VARCHAR NOT NULL,
	archived_at DATETIME,
	sku VARCHAR,
	library_file_id INTEGER NOT NULL,
	plate_id INTEGER NOT NULL,
	file_name VARCHAR NOT NULL,
	file_fingerprint VARCHAR,
	units_per_print INTEGER DEFAULT '1' NOT NULL,
	min_stock INTEGER DEFAULT '0' NOT NULL,
	buffer INTEGER DEFAULT '0' NOT NULL,
	target_model VARCHAR,
	printer_id INTEGER,
	needs_assembly BOOLEAN DEFAULT '0' NOT NULL,
	auto_enabled BOOLEAN DEFAULT '0' NOT NULL,
	paused BOOLEAN DEFAULT '0' NOT NULL,
	pause_reason VARCHAR,
	consecutive_failures INTEGER DEFAULT '0' NOT NULL,
	stock INTEGER DEFAULT '0' NOT NULL,
	reserved INTEGER DEFAULT '0' NOT NULL,
	provisional INTEGER DEFAULT '0' NOT NULL,
	awaiting_assembly INTEGER DEFAULT '0' NOT NULL,
	thumbnail BLOB,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, label_text VARCHAR,
	PRIMARY KEY (id),
	CONSTRAINT ck_articles_stock CHECK (stock >= 0),
	CONSTRAINT ck_articles_reserved CHECK (reserved >= 0),
	CONSTRAINT ck_articles_provisional CHECK (provisional >= 0),
	CONSTRAINT ck_articles_awaiting_assembly CHECK (awaiting_assembly >= 0),
	CONSTRAINT ck_articles_units_per_print CHECK (units_per_print >= 1),
	UNIQUE (sku)
)"""


def _plate_index() -> None:
    op.create_index(
        "uq_articles_active_plate", "articles", ["library_file_id", "plate_id"], unique=True,
        sqlite_where=sa.text("archived_at IS NULL"),
    )


def upgrade() -> None:
    op.execute(NEW_TABLE)
    op.execute(f"INSERT INTO _alembic_tmp_articles ({OLD_COLUMNS}) SELECT {OLD_COLUMNS} FROM articles")
    op.execute("DROP TABLE articles")
    op.execute("ALTER TABLE _alembic_tmp_articles RENAME TO articles")
    _plate_index()
    op.create_index(
        "uq_articles_variant_key", "articles", ["parent_id", "variant_key"], unique=True,
        sqlite_where=sa.text("archived_at IS NULL"),
    )

    op.create_table(
        "article_options",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("article_id", sa.Integer(), sa.ForeignKey("articles.id"), nullable=False),
        sa.Column("field", sa.Integer(), nullable=False),
        sa.Column("sort", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("suffix", sa.String(), nullable=False),
        sa.CheckConstraint("field IN (1, 2)", name="ck_article_options_field"),
        sa.CheckConstraint("suffix <> ''", name="ck_article_options_suffix"),
    )
    op.create_table(
        "article_components",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("article_id", sa.Integer(), sa.ForeignKey("articles.id"), nullable=False),
        sa.Column("field", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("option_id", sa.Integer(), sa.ForeignKey("article_options.id"), nullable=True),
        sa.Column("part_id", sa.Integer(), sa.ForeignKey("articles.id"), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("sort", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("qty >= 1", name="ck_article_components_qty"),
        sa.CheckConstraint("field IN (0, 1, 2)", name="ck_article_components_field"),
        sa.CheckConstraint("(field = 0) = (option_id IS NULL)", name="ck_article_components_option"),
    )
    op.create_index("ix_article_components_part", "article_components", ["part_id"])
    op.execute("ALTER TABLE order_lines ADD COLUMN assembled_id INTEGER REFERENCES articles (id)")


def downgrade() -> None:
    """Only while every article is a print article: the old table cannot hold the others."""
    conn = op.get_bind()
    if conn.execute(sa.text("SELECT count(*) FROM articles WHERE kind <> 'print'")).scalar():
        raise RuntimeError("cannot downgrade below 0018: assembled articles or variants exist")
    with op.batch_alter_table("order_lines") as batch:
        batch.drop_column("assembled_id")
    op.drop_index("ix_article_components_part", table_name="article_components")
    op.drop_table("article_components")
    op.drop_table("article_options")
    op.execute(OLD_TABLE)
    op.execute(f"INSERT INTO _alembic_tmp_articles ({OLD_COLUMNS}) SELECT {OLD_COLUMNS} FROM articles")
    op.execute("DROP TABLE articles")
    op.execute("ALTER TABLE _alembic_tmp_articles RENAME TO articles")
    _plate_index()
