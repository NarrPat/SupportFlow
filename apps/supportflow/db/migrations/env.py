"""Alembic shares the caller's transaction and advisory lock."""
from alembic import context


context.configure(
    connection=context.config.attributes["connection"],
    version_table_schema="public",
)
with context.begin_transaction():
    context.run_migrations()
