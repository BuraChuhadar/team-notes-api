from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from shutil import copytree
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import (
    MetaData,
    String,
    Table,
    column,
    func,
    inspect,
    select,
    table,
    update,
)
from sqlalchemy.engine import Connection

import app.database as database
from app.models import Base, Note
from test_database import PostgreSQLTestCase

MIGRATION_PATH = Path(__file__).resolve().parents[1] / "app" / "migrations"
ALEMBIC_VERSION = table("alembic_version", column("version_num", String(32)))


class MigrationTests(PostgreSQLTestCase):
    def current_revision(self) -> str | None:
        with database.get_engine().connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()

    @contextmanager
    def extra_revision(self, body: str) -> Generator[None, None, None]:
        with TemporaryDirectory() as directory:
            copytree(MIGRATION_PATH, directory, dirs_exist_ok=True)
            (Path(directory) / "versions" / "002_test.py").write_text(
                "from alembic import op\nimport sqlalchemy as sa\n"
                'revision = "002_test"\ndown_revision = "001_initial"\n'
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
        self.assertEqual(self.current_revision(), "001_initial")
        with database.get_engine().connect() as connection:
            self.assertTrue(
                {"users", "teams", "memberships", "notes", "alembic_version"}
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
        self.assertEqual(self.current_revision(), "001_initial")
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
            )
        with self.extra_revision(
            'op.add_column("notes", sa.Column("version", sa.Integer(), nullable=False, server_default=sa.literal(1)))'
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
                ),
                original,
            )
        with database.get_engine().connect() as connection:
            notes = Table("notes", MetaData(), autoload_with=connection)
            self.assertEqual(
                connection.execute(select(notes.c.version)).scalar_one(), 1
            )
        self.assertEqual(self.current_revision(), "002_test")

    def test_database_from_newer_app_is_rejected_without_changes(self) -> None:
        database.init_database()
        with database.get_engine().begin() as connection:
            connection.execute(
                update(ALEMBIC_VERSION).values(version_num="future_revision")
            )
        with self.assertRaises(RuntimeError):
            database.init_database()
        self.assertEqual(self.current_revision(), "future_revision")

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
        self.assertEqual(self.current_revision(), "001_initial")
        with database.get_session() as session:
            self.assertEqual(
                session.scalars(select(Note.title)).one(), "Keep this note"
            )


if __name__ == "__main__":
    unittest.main()
