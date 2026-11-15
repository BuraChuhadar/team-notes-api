"""Delete expired authentication data in short, bounded transactions."""

import argparse
import json
from datetime import timedelta

from sqlalchemy import delete, func, select

from app.database import check_database_ready, get_engine, get_session
from app.models import AuthSession, LoginThrottle
from app.rate_limit import get_rate_limit_settings


def cleanup_expired(
    *, batch_size: int = 1000, max_batches: int = 100
) -> dict[str, int]:
    """Clean expired rows while concurrent maintenance skips locked batches."""
    if not 1 <= batch_size <= 10_000 or not 1 <= max_batches <= 1000:
        raise ValueError("batch_size must be 1–10000 and max_batches must be 1–1000.")
    totals = {"sessions_deleted": 0, "throttle_windows_deleted": 0}
    window = timedelta(seconds=get_rate_limit_settings().window_seconds)
    for table, primary_key, expires_at, condition, result_key in (
        (
            AuthSession,
            AuthSession.token_hash,
            AuthSession.expires_at,
            AuthSession.expires_at <= func.now(),
            "sessions_deleted",
        ),
        (
            LoginThrottle,
            LoginThrottle.subject_hash,
            LoginThrottle.window_started_at,
            LoginThrottle.window_started_at <= func.now() - window,
            "throttle_windows_deleted",
        ),
    ):
        for _ in range(max_batches):
            with get_session() as session:
                candidates = (
                    select(primary_key)
                    .where(condition)
                    .order_by(expires_at, primary_key)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
                removed = session.scalars(
                    delete(table)
                    .where(primary_key.in_(candidates))
                    .returning(primary_key)
                ).all()
            totals[result_key] += len(removed)
            if len(removed) < batch_size:
                break
    return totals


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--max-batches", type=int, default=100)
    arguments = parser.parse_args()
    engine = get_engine()
    try:
        check_database_ready()
        result = cleanup_expired(
            batch_size=arguments.batch_size, max_batches=arguments.max_batches
        )
        print(json.dumps(result, sort_keys=True))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
