"""Run Alembic on the transaction supplied by the application."""

from alembic import context
from sqlalchemy.engine import Connection

from app.models import Base

config = context.config
connection = config.attributes.get("connection")
if not isinstance(connection, Connection):
    raise RuntimeError("Run migrations with app.database.migration_config(connection).")

context.configure(
    connection=connection,
    target_metadata=Base.metadata,
    compare_type=True,
)
with context.begin_transaction():
    context.run_migrations()
