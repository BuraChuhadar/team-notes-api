"""Validated deployment settings; environment files are never loaded implicitly."""

import math
import os
import re
from dataclasses import dataclass


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer.") from error
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}.")
    return value


def _seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError as error:
        raise RuntimeError(f"{name} must be a number of seconds.") from error
    if not math.isfinite(value) or not 0.05 <= value <= 60:
        raise RuntimeError(f"{name} must be between 0.05 and 60 seconds.")
    return value


def _boolean(name: str, default: bool) -> bool:
    value = os.environ.get(name, str(default)).strip().lower()
    if value in {"true", "1"}:
        return True
    if value in {"false", "0"}:
        return False
    raise RuntimeError(f"{name} must be true, false, 1, or 0.")


@dataclass(frozen=True)
class Settings:
    db_pool_size: int
    db_max_overflow: int
    db_pool_timeout_seconds: float
    db_connect_timeout_seconds: int
    db_statement_timeout_ms: int
    db_lock_timeout_ms: int
    db_idle_transaction_timeout_ms: int
    db_migration_timeout_ms: int
    db_pool_recycle_seconds: int
    db_trgm_schema: str
    db_application_name: str
    db_compatible_revisions: tuple[str, ...]
    auto_migrate: bool

    @property
    def db_trigram_schema(self) -> str:
        """Readable alias for callers using the full extension name."""
        return self.db_trgm_schema


def get_settings() -> Settings:
    """Read settings afresh so validation and engine cache keys agree."""
    trigram_schema = os.environ.get("DB_TRIGRAM_SCHEMA", "notes_extensions")
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", trigram_schema):
        raise RuntimeError(
            "DB_TRIGRAM_SCHEMA must be a lowercase PostgreSQL identifier."
        )
    application_name = os.environ.get("DB_APPLICATION_NAME", "notes-api")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,63}", application_name):
        raise RuntimeError(
            "DB_APPLICATION_NAME must contain 1–63 letters, numbers, dots, underscores, or hyphens."
        )
    compatible_value = os.environ.get("DB_COMPATIBLE_REVISIONS", "").strip()
    compatible_revisions: tuple[str, ...] = ()
    if compatible_value:
        revision_ids = tuple(value.strip() for value in compatible_value.split(","))
        if any(
            not re.fullmatch(r"[A-Za-z0-9_]{1,32}", value) for value in revision_ids
        ):
            raise RuntimeError(
                "DB_COMPATIBLE_REVISIONS must be comma-separated revision IDs "
                "of 1–32 letters, numbers, or underscores."
            )
        compatible_revisions = tuple(dict.fromkeys(revision_ids))
    return Settings(
        db_pool_size=_integer("DB_POOL_SIZE", 5, 1, 100),
        db_max_overflow=_integer("DB_MAX_OVERFLOW", 5, 0, 100),
        db_pool_timeout_seconds=_seconds("DB_POOL_TIMEOUT_SECONDS", 3),
        db_connect_timeout_seconds=_integer("DB_CONNECT_TIMEOUT_SECONDS", 5, 1, 60),
        db_statement_timeout_ms=_integer(
            "DB_STATEMENT_TIMEOUT_MS", 10_000, 1, 3_600_000
        ),
        db_lock_timeout_ms=_integer("DB_LOCK_TIMEOUT_MS", 3_000, 1, 3_600_000),
        db_idle_transaction_timeout_ms=_integer(
            "DB_IDLE_TRANSACTION_TIMEOUT_MS", 15_000, 1, 3_600_000
        ),
        db_migration_timeout_ms=_integer(
            "DB_MIGRATION_TIMEOUT_MS", 120_000, 1_000, 3_600_000
        ),
        db_pool_recycle_seconds=_integer("DB_POOL_RECYCLE_SECONDS", 1_800, 1, 86_400),
        db_trgm_schema=trigram_schema,
        db_application_name=application_name,
        db_compatible_revisions=compatible_revisions,
        auto_migrate=_boolean("AUTO_MIGRATE", True),
    )
