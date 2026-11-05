"""Alembic environment. Migrations run on a connection handed in by `db.upgrade`."""

from alembic import context

from reprintbuddy.models import Base

config = context.config
connection = config.attributes.get("connection")

if connection is None:
    raise RuntimeError("Migrations are run through reprintbuddy.db.upgrade()")

# Batch mode lets later revisions alter SQLite tables (it rebuilds them).
context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)

with context.begin_transaction():
    context.run_migrations()
