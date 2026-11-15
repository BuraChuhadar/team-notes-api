"""Frozen compatibility checks for the two pre-Alembic database versions.

These contracts describe historical schemas, independently of today's ORM.
They authorize adoption, never infer a version from tables or repair schemas.
"""

from typing import Never

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.script.revision import RevisionError
from alembic.util import CommandError
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    Text,
    column,
    func,
    inspect,
    select,
)
from sqlalchemy.engine import Connection

LEGACY_REVISIONS = {1: "001_initial", 2: "002_sessions"}
# This archival ledger is never created on fresh installs or ORM-managed.
_LEGACY_MIGRATIONS = Table(
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
APPLICATION_TABLES = frozenset(
    {
        "users",
        "teams",
        "memberships",
        "notes",
        "sessions",
        "note_shares",
        "login_throttles",
    }
)

# Column kind and nullability are intentionally explicit historical facts.
_COLUMNS = {
    "users": {
        "id": ("bigint", False),
        "email": ("text", False),
        "display_name": ("text", False),
        "password_hash": ("text", False),
        "created_at": ("timestamptz", False),
    },
    "teams": {
        "id": ("bigint", False),
        "name": ("text", False),
        "created_at": ("timestamptz", False),
    },
    "memberships": {
        "team_id": ("bigint", False),
        "user_id": ("bigint", False),
        "role": ("text", False),
        "created_at": ("timestamptz", False),
    },
    "notes": {
        "id": ("bigint", False),
        "owner_id": ("bigint", False),
        "team_id": ("bigint", True),
        "title": ("text", False),
        "content": ("text", False),
        "created_at": ("timestamptz", False),
        "updated_at": ("timestamptz", False),
    },
    "sessions": {
        "token_hash": ("text", False),
        "user_id": ("bigint", False),
        "created_at": ("timestamptz", False),
        "expires_at": ("timestamptz", False),
    },
}
_PRIMARY_KEYS = {
    "users": ("id",),
    "teams": ("id",),
    "memberships": ("team_id", "user_id"),
    "notes": ("id",),
    "sessions": ("token_hash",),
}
_FOREIGN_KEYS = {
    "users": set(),
    "teams": set(),
    "memberships": {
        (("team_id",), "teams", ("id",), "CASCADE"),
        (("user_id",), "users", ("id",), "CASCADE"),
    },
    "notes": {
        (("owner_id",), "users", ("id",), "RESTRICT"),
        (("team_id",), "teams", ("id",), "RESTRICT"),
    },
    "sessions": {(("user_id",), "users", ("id",), "CASCADE")},
}
_INDEXES = {
    "users": {"users_email_key": ((None,), True)},
    "teams": {},
    "memberships": {"memberships_user_id_idx": (("user_id",), False)},
    "notes": {
        "notes_owner_id_idx": (("owner_id",), False),
        "notes_team_id_idx": (("team_id",), False),
    },
    "sessions": {
        "sessions_user_id_idx": (("user_id",), False),
        "sessions_expires_at_idx": (("expires_at",), False),
    },
}
_CHECKS = {
    "users": {
        "users_email_check": "email = btrim(email) AND email <> ''::text",
        "users_display_name_check": "length(btrim(display_name)) > 0",
        "users_password_hash_check": "length(btrim(password_hash)) > 0",
    },
    "teams": {"teams_name_check": "length(btrim(name)) > 0"},
    "memberships": {
        "memberships_role_check": "role = ANY (ARRAY['owner'::text, 'member'::text])"
    },
    "notes": {"notes_title_check": "length(btrim(title)) > 0"},
    "sessions": {
        "sessions_token_hash_check": "token_hash ~ '^[0-9a-f]{64}$'::text",
        "sessions_check": "expires_at > created_at",
    },
}
_LATER_INDEXES = frozenset(
    {
        "notes_created_id_idx",
        "notes_owner_created_id_idx",
        "notes_team_created_id_idx",
        "notes_title_trgm_idx",
        "notes_content_trgm_idx",
    }
)


def migration_chain(scripts: ScriptDirectory) -> tuple[str, ...]:
    """Return exact revision IDs, newest first, only for a single linear chain."""
    try:
        heads = scripts.get_heads()
        revisions = list(scripts.walk_revisions())
    except (CommandError, RevisionError) as error:
        raise RuntimeError(
            "Repository migration history must be a single linear chain."
        ) from error
    if len(heads) != 1 or not revisions:
        raise RuntimeError("Repository migration history must have exactly one head.")
    for position, revision in enumerate(revisions):
        expected_parent = (
            revisions[position + 1].revision if position + 1 < len(revisions) else None
        )
        if (
            revision.down_revision != expected_parent
            or revision.dependencies is not None
        ):
            raise RuntimeError(
                "Repository migration history must be a single linear chain."
            )
    return tuple(revision.revision for revision in revisions)


def require_unversioned_schema_is_empty(connection: Connection) -> None:
    """Do not replay initial DDL against application tables with no valid ledger."""
    inspector = inspect(connection)
    if APPLICATION_TABLES.intersection(inspector.get_table_names()):
        raise RuntimeError(
            "Unversioned application tables exist; restore valid migration history "
            "or review the schema before upgrading."
        )


def adopt_legacy_migrations(
    connection: Connection,
    config: Config,
    chain: tuple[str, ...],
    current: tuple[str, ...],
) -> None:
    """Validate archival history and stamp only an unversioned, verified schema."""
    inspector = inspect(connection)
    if not inspector.has_table(_LEGACY_MIGRATIONS.name):
        if not current:
            require_unversioned_schema_is_empty(connection)
        return
    versions = list(
        connection.scalars(
            select(_LEGACY_MIGRATIONS.c.version).order_by(_LEGACY_MIGRATIONS.c.version)
        )
    )
    if any(version not in LEGACY_REVISIONS for version in versions):
        raise RuntimeError("Database schema is newer than this service supports.")
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError(
            "Database migration history must be contiguous, starting at version 1."
        )
    if not versions:
        if not current:
            require_unversioned_schema_is_empty(connection)
        return
    legacy_revision = LEGACY_REVISIONS[versions[-1]]
    if current:
        if chain.index(current[0]) > chain.index(legacy_revision):
            raise RuntimeError(
                "Legacy and Alembic migration histories contradict each other."
            )
        # Alembic is authoritative after adoption; the old ledger is archival.
        return
    validate_legacy_schema(connection, versions[-1])
    # An existing but empty Alembic ledger is also unversioned, not evidence
    # that the historical tables need to be recreated.
    command.stamp(config, legacy_revision)


def validate_legacy_schema(connection: Connection, version: int) -> None:
    """Fail closed before stamping a declared v1/v2 schema with missing/drifted DDL."""
    inspector = inspect(connection)
    schema = inspector.default_schema_name
    expected_tables = set(_COLUMNS) - ({"sessions"} if version == 1 else set())
    present_tables = set(inspector.get_table_names())

    def reject(detail: str) -> Never:
        raise RuntimeError(
            f"Legacy schema does not match declared version {version}: {detail}."
        )

    if APPLICATION_TABLES.intersection(present_tables) != expected_tables:
        reject("application tables differ")
    for name in sorted(expected_tables):
        columns = inspector.get_columns(name)
        actual_columns = {}
        for reflected in columns:
            kind = reflected["type"]
            label = (
                "bigint"
                if isinstance(kind, BigInteger)
                else "text"
                if isinstance(kind, Text)
                else "timestamptz"
                if isinstance(kind, DateTime) and kind.timezone
                else "unsupported"
            )
            actual_columns[reflected["name"]] = (label, reflected["nullable"])
            column_name = reflected["name"]
            expected_default = (
                "CURRENT_TIMESTAMP"
                if column_name in {"created_at", "updated_at"}
                else "''::text"
                if name == "notes" and column_name == "content"
                else None
            )
            if reflected.get("default") != expected_default:
                reject(f"{name}.{column_name} default differs")
            identity = reflected.get("identity")
            if name in {"users", "teams", "notes"} and column_name == "id":
                if identity is None or identity.get("always") is not True:
                    reject(f"{name}.id identity differs")
            elif identity is not None:
                reject(f"{name}.{column_name} has an unexpected identity")
        if actual_columns != _COLUMNS[name]:
            reject(f"{name} columns differ")
        if (
            tuple(inspector.get_pk_constraint(name)["constrained_columns"])
            != _PRIMARY_KEYS[name]
        ):
            reject(f"{name} primary key differs")
        foreign_keys = inspector.get_foreign_keys(name)
        if any(key["referred_schema"] not in (None, schema) for key in foreign_keys):
            reject(f"{name} references another schema")
        actual_keys = {
            (
                tuple(key["constrained_columns"]),
                key["referred_table"],
                tuple(key["referred_columns"]),
                key.get("options", {}).get("ondelete"),
            )
            for key in foreign_keys
        }
        if actual_keys != _FOREIGN_KEYS[name]:
            reject(f"{name} foreign keys differ")
        indexes = {index["name"]: index for index in inspector.get_indexes(name)}
        if _LATER_INDEXES.intersection(indexes):
            reject(f"{name} has indexes from a later revision")
        for index_name, (column_names, unique) in _INDEXES[name].items():
            index = indexes.get(index_name)
            if index is None or (tuple(index["column_names"]), index["unique"]) != (
                column_names,
                unique,
            ):
                reject(f"{name} required index {index_name} differs")
            if index.get("dialect_options", {}).get("postgresql_where") is not None:
                reject(f"{name} required index {index_name} is partial")
        if name == "users" and indexes["users_email_key"].get("expressions") != [
            "lower(email)"
        ]:
            reject("users email uniqueness expression differs")
        # Compare known PostgreSQL-deparsed forms, not a general SQL parser or
        # an assumption that equal names imply equal integrity guarantees.
        checks = {
            check["name"]: check["sqltext"]
            for check in inspector.get_check_constraints(name)
        }
        for check_name, definition in _CHECKS[name].items():
            if checks.get(check_name) != definition:
                reject(f"{name} required check {check_name} differs")
