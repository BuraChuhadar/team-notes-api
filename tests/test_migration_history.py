"""Reject ambiguous migration history before changing application tables."""

from pathlib import Path
from shutil import copytree
from tempfile import TemporaryDirectory
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Column, MetaData, String, Table, inspect, select, update
from sqlalchemy.engine import Connection

from app import database
from app.models import Note
from test_database import PostgreSQLTestCase

MIGRATIONS = Path(__file__).resolve().parents[1] / "app" / "migrations"
(HEAD,) = ScriptDirectory(str(MIGRATIONS)).get_heads()


class MigrationHistoryTests(PostgreSQLTestCase):
    def heads(self) -> tuple[str, ...]:
        with database.get_engine().connect() as connection:
            return MigrationContext.configure(connection).get_current_heads()

    def test_symbolic_and_abbreviated_revisions_are_rejected_without_changes(self) -> None:
        database.init_database()
        user_id, _ = self.seed_user_and_team()
        with database.get_session() as session:
            session.add(Note(owner_id=user_id, title="Keep this note"))
        for invalid in ("base", "head", "001", "006_login"):
            with self.subTest(revision=invalid):
                with database.get_engine().begin() as connection:
                    ledger = Table("alembic_version", MetaData(), autoload_with=connection)
                    connection.execute(update(ledger).values(version_num=invalid))
                    before = set(inspect(connection).get_table_names())
                with self.assertRaisesRegex(RuntimeError, "revision"):
                    database.init_database()
                self.assertEqual(self.heads(), (invalid,))
                with database.get_engine().connect() as connection:
                    self.assertEqual(set(inspect(connection).get_table_names()), before)
                with database.get_session() as session:
                    self.assertEqual(session.scalar(select(Note.title)), "Keep this note")

    def test_multiple_current_revisions_are_rejected_without_merging_or_replay(self) -> None:
        database.init_database()
        with database.get_engine().begin() as connection:
            ledger = Table("alembic_version", MetaData(), autoload_with=connection)
            connection.execute(ledger.insert().values(version_num="001_initial"))
            before = set(inspect(connection).get_table_names())
        with self.assertRaisesRegex(RuntimeError, "current revision"):
            database.init_database()
        self.assertEqual(set(self.heads()), {"001_initial", HEAD})
        with database.get_engine().connect() as connection:
            self.assertEqual(set(inspect(connection).get_table_names()), before)

    def test_repository_branch_is_rejected_before_running_its_upgrade(self) -> None:
        database.init_database()
        with TemporaryDirectory() as temporary:
            copytree(MIGRATIONS, temporary, dirs_exist_ok=True)
            (Path(temporary) / "versions" / "branch_probe.py").write_text(
                'revision = "branch_probe"\ndown_revision = "001_initial"\n'
                "branch_labels = None\ndepends_on = None\n"
                "def upgrade():\n    raise AssertionError('Upgrade must not run')\n"
                "def downgrade():\n    raise AssertionError('Downgrade must not run')\n"
            )
            original_config = database.migration_config

            def branch_config(connection: Connection) -> Config:
                config = original_config(connection)
                config.set_main_option("script_location", temporary)
                return config

            with patch.object(database, "migration_config", branch_config):
                with self.assertRaisesRegex(RuntimeError, "head"):
                    database.init_database()
        self.assertEqual(self.heads(), (HEAD,))

    def test_unversioned_tables_are_not_recreated_or_automatically_stamped(self) -> None:
        with database.get_engine().begin() as connection:
            command.upgrade(database.migration_config(connection), "001_initial")
            Table("alembic_version", MetaData(), autoload_with=connection).drop(connection)
        user_id, _ = self.seed_user_and_team()
        with database.get_engine().begin() as connection:
            notes = Table("notes", MetaData(), autoload_with=connection)
            connection.execute(notes.insert().values(owner_id=user_id, title="Untracked note"))
            before = set(inspect(connection).get_table_names())
        with self.assertRaisesRegex(RuntimeError, "Unversioned application tables"):
            database.init_database()
        self.assertEqual(self.heads(), ())
        with database.get_engine().connect() as connection:
            self.assertEqual(set(inspect(connection).get_table_names()), before)
            self.assertEqual(connection.scalar(select(notes.c.title)), "Untracked note")
            self.assertNotIn("version", {column["name"] for column in inspect(connection).get_columns("notes")})

    def test_empty_alembic_ledger_on_empty_schema_can_initialize_normally(self) -> None:
        with database.get_engine().begin() as connection:
            Table("alembic_version", MetaData(), Column("version_num", String(32), primary_key=True)).create(connection)
        database.init_database()
        database.init_database()
        self.assertEqual(self.heads(), (HEAD,))
