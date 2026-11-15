from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, UTC
from hashlib import sha256
from pathlib import Path
from shutil import copytree
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Identity,
    Integer,
    MetaData,
    String,
    Table,
    column,
    delete,
    func,
    inspect,
    literal,
    select,
    table,
    update,
)
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

import app.database as database
from app.main import app
from app.models import AuthSession, Base, Note
from test_database import PostgreSQLTestCase

MIGRATION_PATH = Path(__file__).resolve().parents[1] / "app" / "migrations"
(LATEST_REVISION,) = ScriptDirectory(str(MIGRATION_PATH)).get_heads()
ALEMBIC_VERSION = table("alembic_version", column("version_num", String(32)))


class MigrationTests(PostgreSQLTestCase):
    def current_revision(self) -> str | None:
        with database.get_engine().connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()

    def install_legacy_schema(self, version: int) -> Table:
        """Reproduce an earlier schema and ledger using Python Alembic revisions."""
        revisions = {1: "001_initial", 2: "002_sessions"}
        revision = revisions[version]
        metadata = MetaData()
        ledger = Table(
            "schema_migrations",
            metadata,
            Column("version", Integer, primary_key=True),
            Column(
                "applied_at",
                DateTime(timezone=True),
                nullable=False,
                server_default=func.current_timestamp(),
            ),
            CheckConstraint(column("version") > 0),
        )
        with database.get_engine().begin() as connection:
            command.upgrade(database.migration_config(connection), revision)
            # Model an existing database managed by the older version ledger.
            # Only this test's isolated schema is changed.
            Table("alembic_version", MetaData(), autoload_with=connection).drop(
                connection
            )
            ledger.create(connection)
            connection.execute(
                ledger.insert(),
                [{"version": number} for number in range(1, version + 1)],
            )
        return ledger

    @contextmanager
    def extra_revision(self, body: str) -> Generator[None, None, None]:
        with TemporaryDirectory() as directory:
            copytree(MIGRATION_PATH, directory, dirs_exist_ok=True)
            (Path(directory) / "versions" / "test_probe.py").write_text(
                "from alembic import op\nimport sqlalchemy as sa\n"
                f'revision = "test_probe"\ndown_revision = {LATEST_REVISION!r}\n'
                "branch_labels = None\ndepends_on = None\n\ndef upgrade() -> None:\n"
                + "\n".join("    " + line for line in body.splitlines())
                + "\n\ndef downgrade() -> None:\n    raise NotImplementedError\n",
                encoding="utf-8",
            )
            original_config = database.migration_config

            def temporary_config(connection: Connection) -> Config:
                config = original_config(connection)
                config.set_main_option("script_location", directory)
                return config

            with patch.object(database, "migration_config", temporary_config):
                yield

    def test_fresh_schema_is_versioned_and_initialization_is_repeatable(self) -> None:
        database.init_database()
        database.init_database()
        self.assertEqual(self.current_revision(), LATEST_REVISION)
        with database.get_engine().connect() as connection:
            self.assertTrue(
                {
                    "users",
                    "teams",
                    "memberships",
                    "notes",
                    "sessions",
                    "alembic_version",
                }
                <= set(inspect(connection).get_table_names(schema=self.schema))
            )

    def test_migrated_schema_matches_orm_models(self) -> None:
        database.init_database()
        with database.get_engine().connect() as connection:
            context = MigrationContext.configure(
                connection, opts={"compare_type": True}
            )
            self.assertEqual(compare_metadata(context, Base.metadata), [])

    def test_concurrent_initializers_apply_each_migration_once(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(database.init_database) for _ in range(2)]
            for future in futures:
                future.result(timeout=15)
        self.assertEqual(self.current_revision(), LATEST_REVISION)
        with database.get_engine().connect() as connection:
            self.assertEqual(
                connection.execute(
                    select(func.count()).select_from(ALEMBIC_VERSION)
                ).scalar_one(),
                1,
            )

    def test_incremental_migration_preserves_existing_note_data(self) -> None:
        database.init_database()
        user_id, team_id = self.seed_user_and_team()
        with database.get_session() as session:
            note = Note(
                owner_id=user_id,
                team_id=team_id,
                title="Existing note",
                content="Preserve this content",
            )
            session.add(note)
            session.flush()
            original = (
                note.id,
                note.owner_id,
                note.team_id,
                note.title,
                note.content,
                note.created_at,
                note.updated_at,
                note.version,
            )
        with self.extra_revision(
            'op.add_column("notes", sa.Column("migration_marker", sa.Integer(), nullable=False, server_default=sa.literal(1)))'
        ):
            database.init_database()
            database.init_database()
        with database.get_session() as session:
            upgraded = session.scalars(select(Note)).one()
            self.assertEqual(
                (
                    upgraded.id,
                    upgraded.owner_id,
                    upgraded.team_id,
                    upgraded.title,
                    upgraded.content,
                    upgraded.created_at,
                    upgraded.updated_at,
                    upgraded.version,
                ),
                original,
            )
        with database.get_engine().connect() as connection:
            notes = Table("notes", MetaData(), autoload_with=connection)
            self.assertEqual(
                connection.execute(select(notes.c.migration_marker)).scalar_one(), 1
            )
        self.assertEqual(self.current_revision(), "test_probe")

    def test_database_from_newer_app_is_rejected_without_changes(self) -> None:
        database.init_database()
        with database.get_engine().begin() as connection:
            connection.execute(
                update(ALEMBIC_VERSION).values(version_num="future_revision")
            )
        with self.assertRaises(RuntimeError):
            database.init_database()
        self.assertEqual(self.current_revision(), "future_revision")

    def test_legacy_database_from_newer_app_is_rejected_without_changes(self) -> None:
        ledger = self.install_legacy_schema(2)
        with database.get_engine().begin() as connection:
            connection.execute(ledger.insert().values(version=999))
        with self.assertRaises(RuntimeError):
            database.init_database()
        self.assertIsNone(self.current_revision())
        with database.get_engine().connect() as connection:
            self.assertEqual(
                list(
                    connection.scalars(
                        select(ledger.c.version).order_by(ledger.c.version)
                    )
                ),
                [1, 2, 999],
            )

    def test_legacy_migration_history_with_a_gap_is_rejected(self) -> None:
        ledger = self.install_legacy_schema(2)
        with database.get_engine().begin() as connection:
            connection.execute(delete(ledger).where(ledger.c.version == 1))
        with self.assertRaises(RuntimeError):
            database.init_database()
        self.assertIsNone(self.current_revision())

    def assert_legacy_schema_is_rejected_without_changes(self) -> None:
        def schema_shape() -> dict[str, list[tuple[str, str, bool, str | None]]]:
            with database.get_engine().connect() as connection:
                inspector = inspect(connection)
                return {
                    name: [
                        (
                            column["name"],
                            str(column["type"]),
                            column["nullable"],
                            column.get("default"),
                        )
                        for column in inspector.get_columns(name)
                    ]
                    for name in inspector.get_table_names()
                }

        before = schema_shape()
        with self.assertRaisesRegex(
            RuntimeError, "Legacy schema does not match declared version"
        ):
            database.init_database()
        self.assertIsNone(self.current_revision())
        self.assertEqual(schema_shape(), before)

    def test_legacy_schema_with_missing_sessions_is_not_stamped(self) -> None:
        self.install_legacy_schema(2)
        with database.get_engine().begin() as connection:
            Operations(MigrationContext.configure(connection)).drop_table("sessions")
        self.assert_legacy_schema_is_rejected_without_changes()

    def test_legacy_schema_with_missing_required_index_is_not_stamped(self) -> None:
        self.install_legacy_schema(2)
        with database.get_engine().begin() as connection:
            Operations(MigrationContext.configure(connection)).drop_index(
                "notes_owner_id_idx", table_name="notes"
            )
        self.assert_legacy_schema_is_rejected_without_changes()

    def test_legacy_schema_requires_unique_case_insensitive_email_index(self) -> None:
        self.install_legacy_schema(2)
        with database.get_engine().begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            operations.drop_index("users_email_key", table_name="users")
            operations.create_index(
                "users_email_key", "users", [func.lower(column("email"))]
            )
        self.assert_legacy_schema_is_rejected_without_changes()

    def test_legacy_schema_with_missing_check_is_not_stamped(self) -> None:
        self.install_legacy_schema(2)
        with database.get_engine().begin() as connection:
            Operations(MigrationContext.configure(connection)).drop_constraint(
                "notes_title_check", "notes", type_="check"
            )
        self.assert_legacy_schema_is_rejected_without_changes()

    def test_legacy_check_name_does_not_authorize_a_changed_predicate(self) -> None:
        self.install_legacy_schema(2)
        with database.get_engine().begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            operations.drop_constraint("notes_title_check", "notes", type_="check")
            operations.create_check_constraint(
                "notes_title_check", "notes", literal(True)
            )
        self.assert_legacy_schema_is_rejected_without_changes()

    def test_legacy_schema_with_missing_foreign_key_is_not_stamped(self) -> None:
        self.install_legacy_schema(2)
        with database.get_engine().begin() as connection:
            Operations(MigrationContext.configure(connection)).drop_constraint(
                "notes_owner_id_fkey", "notes", type_="foreignkey"
            )
        self.assert_legacy_schema_is_rejected_without_changes()

    def test_legacy_schema_with_missing_primary_key_is_not_stamped(self) -> None:
        self.install_legacy_schema(2)
        with database.get_engine().begin() as connection:
            Operations(MigrationContext.configure(connection)).drop_constraint(
                "sessions_pkey", "sessions", type_="primary"
            )
        self.assert_legacy_schema_is_rejected_without_changes()

    def test_legacy_schema_with_changed_column_type_is_not_stamped(self) -> None:
        self.install_legacy_schema(2)
        with database.get_engine().begin() as connection:
            Operations(MigrationContext.configure(connection)).alter_column(
                "notes", "title", type_=String(100)
            )
        self.assert_legacy_schema_is_rejected_without_changes()

    def test_legacy_schema_with_changed_nullability_is_not_stamped(self) -> None:
        self.install_legacy_schema(2)
        with database.get_engine().begin() as connection:
            Operations(MigrationContext.configure(connection)).alter_column(
                "notes", "content", nullable=True
            )
        self.assert_legacy_schema_is_rejected_without_changes()

    def test_legacy_schema_requires_content_default(self) -> None:
        self.install_legacy_schema(2)
        with database.get_engine().begin() as connection:
            Operations(MigrationContext.configure(connection)).alter_column(
                "notes", "content", server_default=None
            )
        self.assert_legacy_schema_is_rejected_without_changes()

    def test_legacy_schema_requires_timestamp_defaults(self) -> None:
        self.install_legacy_schema(2)
        with database.get_engine().begin() as connection:
            Operations(MigrationContext.configure(connection)).alter_column(
                "users", "created_at", server_default=None
            )
        self.assert_legacy_schema_is_rejected_without_changes()

    def test_legacy_schema_requires_always_generated_identifiers(self) -> None:
        self.install_legacy_schema(2)
        with database.get_engine().begin() as connection:
            Operations(MigrationContext.configure(connection)).alter_column(
                "users",
                "id",
                server_default=Identity(always=False),
                existing_server_default=Identity(always=True),
            )
        self.assert_legacy_schema_is_rejected_without_changes()

    def test_legacy_schema_with_later_note_version_is_not_blindly_adopted(self) -> None:
        self.install_legacy_schema(2)
        with database.get_engine().begin() as connection:
            Operations(MigrationContext.configure(connection)).add_column(
                "notes",
                Column(
                    "version", BigInteger, nullable=False, server_default=literal(1)
                ),
            )
        self.assert_legacy_schema_is_rejected_without_changes()

    def test_legacy_schema_with_later_application_table_is_not_blindly_adopted(
        self,
    ) -> None:
        self.install_legacy_schema(2)
        with database.get_engine().begin() as connection:
            Table(
                "note_shares", MetaData(), Column("id", Integer, primary_key=True)
            ).create(connection)
        self.assert_legacy_schema_is_rejected_without_changes()

    def test_empty_alembic_ledger_adopts_valid_legacy_schema_and_preserves_data(
        self,
    ) -> None:
        ledger = self.install_legacy_schema(2)
        user_id, _ = self.seed_user_and_team()
        token = "legacy-session-with-empty-alembic-ledger"
        with database.get_engine().begin() as connection:
            previous_notes = Table("notes", MetaData(), autoload_with=connection)
            connection.execute(
                previous_notes.insert().values(
                    owner_id=user_id, title="Preserve before adoption"
                )
            )
            Table(
                "alembic_version",
                MetaData(),
                Column("version_num", String(32), primary_key=True),
            ).create(connection)
        with database.get_session() as session:
            session.add(
                AuthSession(
                    user_id=user_id,
                    token_hash=sha256(token.encode()).hexdigest(),
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
        database.init_database()
        database.init_database()
        self.assertEqual(self.current_revision(), LATEST_REVISION)
        with database.get_session() as session:
            self.assertEqual(
                session.scalars(select(Note.title)).one(), "Preserve before adoption"
            )
            self.assertEqual(
                list(
                    session.scalars(select(ledger.c.version).order_by(ledger.c.version))
                ),
                [1, 2],
            )
        with TestClient(app) as client:
            response = client.get(
                "/auth/me", headers={"Authorization": f"Bearer {token}"}
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["id"], user_id)

    def test_contradictory_dual_ledgers_are_rejected_before_applying_ddl(self) -> None:
        ledger = self.install_legacy_schema(2)
        with database.get_engine().begin() as connection:
            command.stamp(database.migration_config(connection), "001_initial")
        with self.assertRaisesRegex(RuntimeError, "histories contradict"):
            database.init_database()
        self.assertEqual(self.current_revision(), "001_initial")
        with database.get_engine().connect() as connection:
            self.assertNotIn(
                "version",
                {column["name"] for column in inspect(connection).get_columns("notes")},
            )
            self.assertEqual(
                list(
                    connection.scalars(
                        select(ledger.c.version).order_by(ledger.c.version)
                    )
                ),
                [1, 2],
            )

    def test_consistent_dual_ledgers_upgrade_with_archival_history_unchanged(
        self,
    ) -> None:
        ledger = self.install_legacy_schema(2)
        with database.get_engine().begin() as connection:
            command.stamp(database.migration_config(connection), "002_sessions")
        database.init_database()
        database.init_database()
        self.assertEqual(self.current_revision(), LATEST_REVISION)
        with database.get_engine().connect() as connection:
            self.assertEqual(
                list(
                    connection.scalars(
                        select(ledger.c.version).order_by(ledger.c.version)
                    )
                ),
                [1, 2],
            )

    def test_failed_migration_preserves_prior_data_and_schema_version(self) -> None:
        database.init_database()
        user_id, _ = self.seed_user_and_team()
        with database.get_session() as session:
            session.add(Note(owner_id=user_id, title="Keep this note"))
        with self.extra_revision(
            'op.create_table("migration_probe", sa.Column("id", sa.Integer(), primary_key=True))\nraise RuntimeError("broken migration")'
        ):
            with self.assertRaisesRegex(RuntimeError, "broken migration"):
                database.init_database()
        with database.get_engine().connect() as connection:
            self.assertFalse(
                inspect(connection).has_table("migration_probe", schema=self.schema)
            )
        self.assertEqual(self.current_revision(), LATEST_REVISION)
        with database.get_session() as session:
            self.assertEqual(
                session.scalars(select(Note.title)).one(), "Keep this note"
            )

    def test_legacy_v1_is_upgraded_without_recreating_users_or_notes(self) -> None:
        ledger = self.install_legacy_schema(1)
        user_id, team_id = self.seed_user_and_team()
        with database.get_engine().begin() as connection:
            legacy_notes = Table("notes", MetaData(), autoload_with=connection)
            note_id = connection.execute(
                legacy_notes.insert()
                .values(
                    owner_id=user_id,
                    team_id=team_id,
                    title="Old note",
                    content="Keep the original",
                )
                .returning(legacy_notes.c.id)
            ).scalar_one()
        database.init_database()
        database.init_database()
        self.assertEqual(self.current_revision(), LATEST_REVISION)
        with database.get_session() as session:
            preserved = session.get(Note, note_id)
            assert preserved is not None
            self.assertEqual(
                (
                    preserved.owner_id,
                    preserved.team_id,
                    preserved.title,
                    preserved.content,
                ),
                (user_id, team_id, "Old note", "Keep the original"),
            )
            self.assertEqual(
                session.scalar(select(func.count()).select_from(AuthSession)), 0
            )
            self.assertEqual(list(session.scalars(select(ledger.c.version))), [1])

    def test_legacy_v2_adoption_preserves_users_notes_and_active_sessions(self) -> None:
        ledger = self.install_legacy_schema(2)
        user_id, _ = self.seed_user_and_team()
        token = "existing-session-token"
        with database.get_engine().begin() as connection:
            legacy_notes = Table("notes", MetaData(), autoload_with=connection)
            connection.execute(
                legacy_notes.insert().values(
                    owner_id=user_id, title="Existing private note"
                )
            )
        with database.get_session() as session:
            session.add(
                AuthSession(
                    user_id=user_id,
                    token_hash=sha256(token.encode()).hexdigest(),
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
        database.init_database()
        self.assertEqual(self.current_revision(), LATEST_REVISION)
        with TestClient(app) as client:
            response = client.get(
                "/auth/me", headers={"Authorization": f"Bearer {token}"}
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["id"], user_id)
        with database.get_session() as session:
            self.assertEqual(
                session.scalars(select(Note.title)).one(), "Existing private note"
            )
            self.assertEqual(
                session.scalar(select(func.count()).select_from(AuthSession)), 1
            )
            self.assertEqual(
                list(
                    session.scalars(select(ledger.c.version).order_by(ledger.c.version))
                ),
                [1, 2],
            )

    def test_note_version_upgrade_preserves_existing_data_and_defaults_new_notes(
        self,
    ) -> None:
        # Seed the actual previous Alembic schema without selecting the new ORM column.
        with database.get_engine().begin() as connection:
            command.upgrade(database.migration_config(connection), "002_sessions")
        user_id, team_id = self.seed_user_and_team()
        with database.get_engine().begin() as connection:
            previous_notes = Table("notes", MetaData(), autoload_with=connection)
            existing = dict(
                connection.execute(
                    previous_notes.insert()
                    .values(
                        owner_id=user_id,
                        team_id=team_id,
                        title="Before versions",
                        content="Keep this content",
                    )
                    .returning(previous_notes)
                )
                .mappings()
                .one()
            )
        database.init_database()
        database.init_database()
        self.assertEqual(self.current_revision(), LATEST_REVISION)
        with database.get_session() as session:
            preserved = session.get(Note, existing["id"])
            assert preserved is not None
            self.assertEqual(
                {name: getattr(preserved, name) for name in existing}, existing
            )
            self.assertEqual(preserved.version, 1)
            new_note = Note(owner_id=user_id, title="After versions")
            session.add(new_note)
            session.flush()
            self.assertEqual(new_note.version, 1)

    def test_nonpositive_note_versions_roll_back_without_changing_saved_notes(
        self,
    ) -> None:
        database.init_database()
        user_id, _ = self.seed_user_and_team()
        with database.get_session() as session:
            saved = Note(owner_id=user_id, title="Keep this note")
            session.add(saved)
            session.flush()
            note_id = saved.id
        for invalid_version in (0, -1):
            with self.subTest(version=invalid_version):
                with self.assertRaises(IntegrityError):
                    with database.get_session() as session:
                        note = session.get(Note, note_id)
                        assert note is not None
                        note.title = "Must roll back"
                        note.version = invalid_version
                        session.flush()
                with database.get_session() as session:
                    note = session.get(Note, note_id)
                    assert note is not None
                    self.assertEqual((note.title, note.version), ("Keep this note", 1))


if __name__ == "__main__":
    unittest.main()
