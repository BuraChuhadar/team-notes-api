"""Database-backed login budgets shared by every API process."""

import hashlib
import math
import os
from dataclasses import dataclass
from datetime import timedelta

from fastapi import HTTPException, status
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert

from app.database import get_session
from app.models import LoginThrottle


@dataclass(frozen=True)
class RateLimitSettings:
    attempts: int = 10
    ip_attempts: int = 50
    window_seconds: int = 60


def _positive_integer(name: str, default: int, maximum: int) -> int:
    value = os.environ.get(name, str(default))
    try:
        parsed = int(value)
    except ValueError:
        raise RuntimeError(
            f"{name} must be an integer between 1 and {maximum}."
        ) from None
    if not 1 <= parsed <= maximum:
        raise RuntimeError(f"{name} must be an integer between 1 and {maximum}.")
    return parsed


def get_rate_limit_settings() -> RateLimitSettings:
    """Read validated settings without loading a local environment file."""
    return RateLimitSettings(
        attempts=_positive_integer("LOGIN_RATE_LIMIT_ATTEMPTS", 10, 10_000),
        ip_attempts=_positive_integer("LOGIN_IP_RATE_LIMIT_ATTEMPTS", 50, 100_000),
        window_seconds=_positive_integer("LOGIN_RATE_LIMIT_WINDOW_SECONDS", 60, 86_400),
    )


def _subject_hash(bucket: str, value: str) -> str:
    return hashlib.sha256(f"login:{bucket}\0{value}".encode()).hexdigest()


def try_consume_login_attempt(email: str, client_host: str) -> None:
    """Reserve both login budgets before password verification.

    PostgreSQL serializes each upsert, and every request locks its hashed keys in
    the same order. Counters commit before an HTTP error is raised, so failed or
    blocked credentials cannot roll back their attempt. Both successful and
    failed sign-ins consume budgets; the caller never supplies a trusted proxy
    header directly to this helper.
    """
    settings = get_rate_limit_settings()
    window = timedelta(seconds=settings.window_seconds)
    subjects = sorted(
        (
            (_subject_hash("account", email.strip().lower()), settings.attempts),
            (_subject_hash("address", client_host), settings.ip_attempts),
        )
    )
    blocked_for = 0.0
    with get_session() as session:
        now = session.execute(select(func.clock_timestamp())).scalar_one()
        for subject_hash, limit in subjects:
            expired = LoginThrottle.window_started_at <= now - window
            statement = (
                insert(LoginThrottle)
                .values(subject_hash=subject_hash, window_started_at=now, attempts=1)
                .on_conflict_do_update(
                    index_elements=[LoginThrottle.subject_hash],
                    set_={
                        "window_started_at": case(
                            (expired, now), else_=LoginThrottle.window_started_at
                        ),
                        "attempts": case(
                            (expired, 1),
                            else_=func.least(LoginThrottle.attempts + 1, limit + 1),
                        ),
                    },
                )
                .returning(LoginThrottle.attempts, LoginThrottle.window_started_at)
            )
            attempts, started_at = session.execute(statement).one()
            if attempts > limit:
                blocked_for = max(
                    blocked_for, (started_at + window - now).total_seconds()
                )

    if blocked_for > 0:
        retry_after = min(settings.window_seconds, max(1, math.ceil(blocked_for)))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Try again later.",
            headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
        )
