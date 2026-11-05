"""Alembic environment. Migrations run on a connection handed in by `db.upgrade`."""

from alembic import context

from lagerbuddy.models import Base

config = context.config
connection = config.attributes.get("connection")

if connection is None:
    raise RuntimeError("Migrations are run through lagerbuddy.db.upgrade()")

context.configure(connection=connection, target_metadata=Base.metadata)

with context.begin_transaction():
    context.run_migrations()
