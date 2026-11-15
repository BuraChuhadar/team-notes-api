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
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    column,
    create_engine,
    func,
    inspect,
    literal,
    select,
)
from sqlalchemy.engine import Connection, Engine, URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings

MIGRATION_LOCK_ID = 0x4E4F544553
_LEGACY_REVISIONS = {1: "001_initial", 2: "002_sessions"}

# Separate metadata preserves this archival ledger without creating it on fresh
# installations or treating it as an ORM-managed application table.
LEGACY_MIGRATIONS = Table(
    "schema_migrations",
    MetaData(),
    Column("version", Integer, primary_key=True),
    Column(
        "applied_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    ),
    CheckConstraint(column("version") > 0),
)


@cache
def _engine_for_url(database_url: URL, settings: Settings) -> Engine:
    existing_options = database_url.query.get("options", "")
    if not isinstance(existing_options, str):
        raise RuntimeError("DATABASE_URL must contain at most one options parameter.")
    options = " ".join(
        filter(
            None,
            (
                existing_options,
                f"-cstatement_timeout={settings.db_statement_timeout_ms}",
                f"-clock_timeout={settings.db_lock_timeout_ms}",
                "-cidle_in_transaction_session_timeout="
                f"{settings.db_idle_transaction_timeout_ms}",
            ),
        )
    )
    return create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_recycle=settings.db_pool_recycle_seconds,
        pool_use_lifo=True,
        connect_args={
            "connect_timeout": settings.db_connect_timeout_seconds,
            "options": options,
            "application_name": settings.db_application_name,
        },
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
    return _engine_for_url(url.set(drivername="postgresql+psycopg"), get_settings())


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Commit successful work, roll back failures, and close each session."""
    with Session(get_engine(), expire_on_commit=False) as session:
        with session.begin():
            yield session


def migration_config(connection: Connection) -> Config:
    """Build an Alembic configuration sharing the caller's transaction."""
    config = Config()
    script_location = str(Path(__file__).parent / "migrations")
    config.set_main_option("script_location", script_location)
    config.attributes["connection"] = connection
    return config


def _bridge_legacy_migrations(connection: Connection, config: Config) -> None:
    inspector = inspect(connection)
    if not inspector.has_table(LEGACY_MIGRATIONS.name):
        return
    versions = list(
        connection.scalars(
            select(LEGACY_MIGRATIONS.c.version).order_by(LEGACY_MIGRATIONS.c.version)
        )
    )
    if any(version not in _LEGACY_REVISIONS for version in versions):
        raise RuntimeError("Database schema is newer than this service supports.")
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError(
            "Database migration history must be contiguous, starting at version 1."
        )
    if versions and not inspector.has_table("alembic_version"):
        # These tables were already created by the previous migration runner.
        command.stamp(config, _LEGACY_REVISIONS[versions[-1]])


def init_database() -> None:
    """Upgrade atomically, preserving legacy schemas and all existing data."""
    with get_engine().begin() as connection:
        # DDL is a release operation, with a larger bounded timeout than requests.
        migration_timeout = str(get_settings().db_migration_timeout_ms)
        for name, value in (
            ("statement_timeout", migration_timeout),
            ("lock_timeout", migration_timeout),
            ("idle_in_transaction_session_timeout", "0"),
        ):
            connection.execute(select(func.set_config(name, value, True)))
        # Serialize startup before inspecting or creating either revision ledger.
        connection.execute(select(func.pg_advisory_xact_lock(MIGRATION_LOCK_ID)))
        config = migration_config(connection)
        _bridge_legacy_migrations(connection, config)
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


def check_database_ready() -> None:
    """Accept this image's schema or an explicitly reviewed compatible revision."""
    try:
        with get_engine().connect() as connection:
            connection.execute(select(literal(1))).scalar_one()
            expected = set(
                ScriptDirectory.from_config(migration_config(connection)).get_heads()
            )
            current = set(MigrationContext.configure(connection).get_current_heads())
            reviewed_compatible = len(current) == 1 and current.issubset(
                get_settings().db_compatible_revisions
            )
            if current != expected and not reviewed_compatible:
                raise RuntimeError(
                    "Database schema is not ready; run the migration job or configure "
                    "an explicitly reviewed compatible revision."
                )
    except SQLAlchemyError as error:
        raise RuntimeError("Database is not ready.") from error
