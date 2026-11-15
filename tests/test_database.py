import os
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg.errors
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateSchema, DropSchema

from app.database import get_engine, get_session, init_database
from app.models import Membership, Note, Team, User


class ConfigurationTests(unittest.TestCase):
    def test_database_url_is_required(self) -> None:
        with patch.dict(os.environ):
            os.environ.pop("DATABASE_URL", None)
            with self.assertRaisesRegex(RuntimeError, "DATABASE_URL"):
                with get_session():
                    self.fail("A session should require DATABASE_URL")


@unittest.skipUnless(
    os.environ.get("TEST_DATABASE_URL"), "Set TEST_DATABASE_URL to run PostgreSQL tests"
)
class PostgreSQLTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.test_url = make_url(os.environ["TEST_DATABASE_URL"]).set(
            drivername="postgresql+psycopg"
        )
        self.admin_engine = create_engine(self.test_url)
        self.addCleanup(self.admin_engine.dispose)
        self.schema = "test_notes_" + uuid4().hex
        with self.admin_engine.begin() as connection:
            connection.execute(CreateSchema(self.schema))
        self.addCleanup(self.drop_schema)
        schema_url = self.test_url.update_query_dict(
            {"options": f"-csearch_path={self.schema}"}
        )
        environment = patch.dict(
            os.environ,
            {"DATABASE_URL": schema_url.render_as_string(hide_password=False)},
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(lambda: get_engine().dispose())

    def drop_schema(self) -> None:
        with self.admin_engine.begin() as connection:
            connection.execute(DropSchema(self.schema, cascade=True))

    def seed_user_and_team(self) -> tuple[int, int]:
        with get_session() as session:
            user = User(
                email="alice@example.test",
                display_name="Alice",
                password_hash="test-password-hash",
            )
            team = Team(name="Team")
            session.add_all([user, team])
            session.flush()
            return user.id, team.id


class DatabaseTests(PostgreSQLTestCase):
    def test_notes_survive_sessions_and_reinitialization(self) -> None:
        init_database()
        user_id, _ = self.seed_user_and_team()
        with get_session() as session:
            connection = session.connection()
            session.add(Note(owner_id=user_id, title="First note"))
        self.assertTrue(connection.closed)
        init_database()
        with get_session() as session:
            note = session.scalars(select(Note)).one()
            self.assertEqual(note.title, "First note")
            self.assertEqual(note.content, "")
            self.assertEqual(note.owner_id, user_id)
            self.assertIsNone(note.team_id)
            self.assertIsNotNone(note.created_at.tzinfo)
            self.assertEqual(note.created_at, note.updated_at)

    def test_failed_write_rolls_back_and_closes_session(self) -> None:
        init_database()
        user_id, _ = self.seed_user_and_team()
        connection: Connection | None = None
        with self.assertRaises(IntegrityError) as failure:
            with get_session() as session:
                connection = session.connection()
                session.add(Note(owner_id=user_id, title="Valid"))
                session.flush()
                session.add(Note(owner_id=user_id, title="   "))
        self.assertIsInstance(failure.exception.orig, psycopg.errors.CheckViolation)
        assert connection is not None
        self.assertTrue(connection.closed)
        with get_session() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Note)), 0)

    def test_user_emails_are_unique_ignoring_case(self) -> None:
        init_database()
        self.seed_user_and_team()
        with self.assertRaises(IntegrityError) as failure:
            with get_session() as session:
                session.add(
                    User(
                        email="ALICE@example.test",
                        display_name="Another Alice",
                        password_hash="another-test-hash",
                    )
                )
        self.assertIsInstance(failure.exception.orig, psycopg.errors.UniqueViolation)

    def test_memberships_enforce_roles_uniqueness_and_references(self) -> None:
        init_database()
        user_id, team_id = self.seed_user_and_team()
        with get_session() as session:
            session.add(Membership(team_id=team_id, user_id=user_id, role="owner"))
        cases = [
            (team_id, user_id, "member", psycopg.errors.UniqueViolation),
            (team_id, user_id, "admin", psycopg.errors.CheckViolation),
            (team_id, -1, "member", psycopg.errors.ForeignKeyViolation),
            (-1, user_id, "member", psycopg.errors.ForeignKeyViolation),
        ]
        for team, user, role, error in cases:
            with self.subTest(team=team, user=user, role=role):
                with self.assertRaises(IntegrityError) as failure:
                    with get_session() as session:
                        session.add(Membership(team_id=team, user_id=user, role=role))
                self.assertIsInstance(failure.exception.orig, error)

    def test_note_ownership_and_deletion_preserve_referential_integrity(self) -> None:
        init_database()
        user_id, team_id = self.seed_user_and_team()
        for owner, team, error in [
            (None, None, psycopg.errors.NotNullViolation),
            (-1, None, psycopg.errors.ForeignKeyViolation),
            (user_id, -1, psycopg.errors.ForeignKeyViolation),
        ]:
            with self.subTest(owner=owner, team=team):
                with self.assertRaises(IntegrityError) as failure:
                    with get_session() as session:
                        session.add(
                            Note(owner_id=owner, team_id=team, title="Invalid note")
                        )
                self.assertIsInstance(failure.exception.orig, error)
        with get_session() as session:
            session.add(Note(owner_id=user_id, team_id=team_id, title="Team note"))
        for statement in (
            delete(User).where(User.id == user_id),
            delete(Team).where(Team.id == team_id),
        ):
            with self.subTest(statement=str(statement)):
                with self.assertRaises(IntegrityError) as failure:
                    with get_session() as session:
                        session.execute(statement)
                self.assertIsInstance(
                    failure.exception.orig, psycopg.errors.RestrictViolation
                )
        with get_session() as session:
            note = session.scalars(select(Note)).one()
            self.assertEqual(
                (note.owner_id, note.team_id, note.title),
                (user_id, team_id, "Team note"),
            )
            self.assertIsNotNone(session.get(User, user_id))
            self.assertIsNotNone(session.get(Team, team_id))


if __name__ == "__main__":
    unittest.main()
