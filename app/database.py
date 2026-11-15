"""Pooled PostgreSQL sessions and transactional Alembic migrations."""

import os
from collections.abc import Generator
from contextlib import contextmanager
from functools import cache
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Connection, Engine, URL, make_url
from sqlalchemy.orm import Session

MIGRATION_LOCK_ID = 0x4E4F544553


@cache
def _engine_for_url(database_url: URL) -> Engine:
    return create_engine(
        database_url, pool_pre_ping=True, connect_args={"connect_timeout": 5}
    )


def get_engine() -> Engine:
    """Reuse one lazy connection pool per configured PostgreSQL URL."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url or not database_url.strip():
        raise RuntimeError(
            "DATABASE_URL is required. Set it to a PostgreSQL connection URL."
        )
    url = make_url(database_url.strip())
    if url.drivername not in {"postgres", "postgresql", "postgresql+psycopg"}:
        raise RuntimeError("DATABASE_URL must use PostgreSQL with the Psycopg driver.")
    return _engine_for_url(url.set(drivername="postgresql+psycopg"))


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Commit successful work, roll back failures, and close each session."""
    with Session(get_engine(), expire_on_commit=False) as session:
        with session.begin():
            yield session


def migration_config(connection: Connection) -> Config:
    """Build an Alembic configuration sharing the caller's transaction."""
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    config.attributes["connection"] = connection
    return config


def init_database() -> None:
    """Apply pending revisions atomically and preserve existing data."""
    with get_engine().begin() as connection:
        connection.execute(select(func.pg_advisory_xact_lock(MIGRATION_LOCK_ID)))
        config = migration_config(connection)
        scripts = ScriptDirectory.from_config(config)
        current_heads = MigrationContext.configure(connection).get_current_heads()
        try:
            for revision in current_heads:
                scripts.get_revision(revision)
        except CommandError as error:
            raise RuntimeError(
                "Database schema is newer than this service supports."
            ) from error
        command.upgrade(config, "head")
