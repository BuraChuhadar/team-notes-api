"""Internal fixture and PostgreSQL sampling helpers for the isolated load test."""

import argparse
from datetime import timedelta
import json
import secrets

from sqlalchemy import BigInteger, Integer, Text, column, func, insert, select, table

from app.database import check_database_ready, get_engine, get_session
from app.models import AuthSession, Membership, Note, NoteShare, Team, User
from app.security import hash_password, hash_token


def seed(users: int, notes: int) -> dict:
    if not 2 <= users <= 100 or not users <= notes <= 100_000:
        raise ValueError(
            "Use 2–100 users and users..100000 notes in this disposable dataset."
        )
    check_database_ready()
    password = secrets.token_urlsafe(20)
    password_hash = hash_password(password)
    accounts = []
    with get_session() as session:
        if session.scalar(select(func.count()).select_from(User)):
            raise ValueError("Benchmark seeding requires an empty disposable database.")
        teams = [Team(name=f"Benchmark team {index}") for index in range(2)]
        session.add_all(teams)
        session.flush()
        for index in range(users):
            user = User(
                email=f"benchmark{index}@example.com",
                display_name=f"Benchmark {index}",
                password_hash=password_hash,
            )
            session.add(user)
            session.flush()
            team = teams[index % 2]
            token = secrets.token_urlsafe(32)
            now = session.scalars(select(func.now())).one()
            session.add_all(
                [
                    Membership(
                        team_id=team.id,
                        user_id=user.id,
                        role="owner" if index < 2 else "member",
                    ),
                    AuthSession(
                        token_hash=hash_token(token),
                        user_id=user.id,
                        expires_at=now + timedelta(hours=1),
                    ),
                ]
            )
            accounts.append({"id": user.id, "email": user.email, "token": token})
        note_values = []
        for index in range(notes):
            account = accounts[index % users]
            private = index % 5 == 0 or index < users
            note_values.append(
                {
                    "owner_id": account["id"],
                    "team_id": None if private else teams[index % 2].id,
                    "title": f"Benchmark note {index}",
                    "content": ("rarebenchmarkneedle " if index % 997 == 0 else "")
                    + ("Meeting decisions and action items. " * 15),
                }
            )
        records = list(
            session.execute(
                insert(Note).returning(Note.id, Note.owner_id, Note.title), note_values
            ).mappings()
        )
        for account in accounts:
            own = next(row for row in records if row["owner_id"] == account["id"])
            account.update(note_id=own["id"], title=own["title"], version=1)
        # A small read-only shared subset exercises the permissions EXISTS branch.
        session.add_all(
            NoteShare(note_id=row["id"], team_id=teams[1].id) for row in records[:users]
        )
    return {"accounts": accounts, "password": password, "users": users, "notes": notes}


def sample() -> dict:
    activity = table(
        "pg_stat_activity",
        column("datname", Text),
        column("state", Text),
        column("wait_event_type", Text),
        schema="pg_catalog",
    )
    locks = table(
        "pg_locks",
        column("database", BigInteger),
        column("granted"),
        schema="pg_catalog",
    )
    with get_session() as session:
        counts = dict(
            session.execute(
                select(activity.c.state, func.count())
                .where(activity.c.datname == func.current_database())
                .group_by(activity.c.state)
            ).all()
        )
        waiting = session.scalar(
            select(func.count())
            .select_from(activity)
            .where(
                activity.c.datname == func.current_database(),
                activity.c.wait_event_type == "Lock",
            )
        )
        database_oid = func.cast(func.current_database(), Text)
        databases = table(
            "pg_database",
            column("oid", Integer),
            column("datname", Text),
            schema="pg_catalog",
        )
        oid = session.scalar(
            select(databases.c.oid).where(databases.c.datname == database_oid)
        )
        blocked = session.scalar(
            select(func.count())
            .select_from(locks)
            .where(locks.c.database == oid, locks.c.granted.is_(False))
        )
    return {
        "connections": sum(counts.values()),
        "states": {str(k): v for k, v in counts.items()},
        "lock_waiting_sessions": waiting or 0,
        "ungranted_database_locks": blocked or 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["seed", "sample"])
    parser.add_argument("--users", type=int, default=8)
    parser.add_argument("--notes", type=int, default=10000)
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                seed(args.users, args.notes) if args.action == "seed" else sample()
            )
        )
    finally:
        get_engine().dispose()


if __name__ == "__main__":
    main()
