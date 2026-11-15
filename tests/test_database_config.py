import os
import secrets
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from shutil import copytree
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

import psycopg.errors
from alembic import command
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import (
    Boolean,
    Column,
    DDL,
    Identity,
    Integer,
    MetaData,
    Table,
    Text,
    column,
    func,
    literal,
    or_,
    select,
    table,
    update,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError, TimeoutError

from app.config import get_settings
from app.database import (
    check_database_ready,
    get_engine,
    get_session,
    init_database,
    migration_config,
)
from app.main import app
from app.models import AuthSession, Base, LoginThrottle, Note, Team
from scripts.cleanup_sessions import cleanup_expired
from test_database import PostgreSQLTestCase


class DeploymentSettingsTests(unittest.TestCase):
    def test_invalid_resource_limits_are_rejected(self) -> None:
        for name, value in (
            ("DB_POOL_SIZE", "0"),
            ("DB_MAX_OVERFLOW", "-1"),
            ("DB_POOL_TIMEOUT_SECONDS", "nan"),
            ("DB_POOL_TIMEOUT_SECONDS", "0"),
            ("DB_STATEMENT_TIMEOUT_MS", "0"),
            ("DB_LOCK_TIMEOUT_MS", "not-a-number"),
            ("AUTO_MIGRATE", "maybe"),
            ("DB_TRIGRAM_SCHEMA", "unsafe.schema"),
            ("DB_APPLICATION_NAME", "too long " * 20),
            ("DB_COMPATIBLE_REVISIONS", "future-revision"),
            ("DB_COMPATIBLE_REVISIONS", "x" * 33),
            ("DB_COMPATIBLE_REVISIONS", "first,,second"),
            ("DB_COMPATIBLE_REVISIONS", ",future"),
        ):
            with (
                self.subTest(name=name, value=value),
                patch.dict(os.environ, {name: value}),
            ):
                with self.assertRaisesRegex(RuntimeError, name):
                    get_settings()

    def test_settings_do_not_require_a_database_url_and_preserve_explicit_migration_mode(
        self,
    ) -> None:
        with patch.dict(
            os.environ, {"AUTO_MIGRATE": "false", "DB_TRIGRAM_SCHEMA": "public"}
        ):
            os.environ.pop("DATABASE_URL", None)
            settings = get_settings()
        self.assertFalse(settings.auto_migrate)
        self.assertEqual(settings.db_trgm_schema, "public")

    def test_compatible_revisions_require_explicit_valid_ids(self) -> None:
        with patch.dict(os.environ, {"DB_COMPATIBLE_REVISIONS": ""}):
            self.assertEqual(get_settings().db_compatible_revisions, ())
        with patch.dict(
            os.environ,
            {"DB_COMPATIBLE_REVISIONS": "007_expand, future_008,007_expand"},
        ):
            self.assertEqual(
                get_settings().db_compatible_revisions, ("007_expand", "future_008")
            )


class DatabaseResourceTests(PostgreSQLTestCase):
    def test_connection_configuration_preserves_isolated_schema_and_timeouts(
        self,
    ) -> None:
        with patch.dict(
            os.environ,
            {
                "DB_STATEMENT_TIMEOUT_MS": "3210",
                "DB_LOCK_TIMEOUT_MS": "1234",
                "DB_IDLE_TRANSACTION_TIMEOUT_MS": "6543",
            },
        ):
            engine = get_engine()
            self.addCleanup(engine.dispose)
            with engine.connect() as connection:
                self.assertEqual(
                    connection.scalar(select(func.current_schema())), self.schema
                )
                values = connection.execute(
                    select(
                        func.current_setting("statement_timeout"),
                        func.current_setting("lock_timeout"),
                        func.current_setting("idle_in_transaction_session_timeout"),
                    )
                ).one()
                self.assertEqual(tuple(values), ("3210ms", "1234ms", "6543ms"))

    def test_changed_pool_budget_gets_a_distinct_engine_and_exhaustion_is_bounded(
        self,
    ) -> None:
        original = get_engine()
        with patch.dict(
            os.environ,
            {
                "DB_POOL_SIZE": "1",
                "DB_MAX_OVERFLOW": "0",
                "DB_POOL_TIMEOUT_SECONDS": "0.05",
            },
        ):
            constrained = get_engine()
            self.addCleanup(constrained.dispose)
            self.assertIsNot(constrained, original)
            with constrained.connect():
                with self.assertRaises(TimeoutError):
                    with constrained.connect():
                        self.fail("An exhausted one-connection pool must time out")
            with constrained.connect() as connection:
                self.assertEqual(
                    connection.scalar(select(func.current_schema())), self.schema
                )

    def test_statement_timeout_rolls_back_the_whole_unit_of_work(self) -> None:
        init_database()
        with patch.dict(os.environ, {"DB_STATEMENT_TIMEOUT_MS": "25"}):
            constrained = get_engine()
            self.addCleanup(constrained.dispose)
            connection: Connection | None = None
            with self.assertRaises(DBAPIError) as failure:
                with get_session() as session:
                    connection = session.connection()
                    session.add(Team(name="Must roll back"))
                    session.flush()
                    session.execute(select(func.pg_sleep(0.2)))
            self.assertIsInstance(failure.exception.orig, psycopg.errors.QueryCanceled)
            assert connection is not None
            self.assertTrue(connection.closed)
        with get_session() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Team)), 0)

    def test_migrations_have_a_separate_timeout_and_readiness_detects_revision_drift(
        self,
    ) -> None:
        with patch.dict(os.environ, {"DB_STATEMENT_TIMEOUT_MS": "25"}):
            engine = get_engine()
            self.addCleanup(engine.dispose)
            init_database()
        check_database_ready()
        with get_engine().begin() as connection:
            command.stamp(migration_config(connection), "001_initial")
        with self.assertRaisesRegex(RuntimeError, "schema is not ready"):
            check_database_ready()

    def test_row_lock_wait_is_bounded_and_the_failed_transaction_rolls_back(
        self,
    ) -> None:
        init_database()
        _, team_id = self.seed_user_and_team()
        with get_session() as holder:
            holder.execute(
                select(Team).where(Team.id == team_id).with_for_update()
            ).scalar_one()
            with patch.dict(os.environ, {"DB_LOCK_TIMEOUT_MS": "25"}):
                engine = get_engine()
                self.addCleanup(engine.dispose)
                with self.assertRaises(DBAPIError) as failure:
                    with get_session() as contender:
                        contender.execute(
                            select(Team).where(Team.id == team_id).with_for_update()
                        ).scalar_one()
                self.assertIsInstance(
                    failure.exception.orig, psycopg.errors.LockNotAvailable
                )
        with get_session() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Team)), 1)

    def test_readiness_does_not_create_a_schema_or_apply_migrations(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "schema is not ready"):
            check_database_ready()
        with get_engine().connect() as connection:
            self.assertIsNone(
                connection.scalar(select(func.to_regclass("alembic_version")))
            )

    def test_unknown_and_multiple_revision_heads_require_safe_explicit_handling(
        self,
    ) -> None:
        init_database()
        with get_engine().begin() as connection:
            version_table = Table(
                "alembic_version", MetaData(), autoload_with=connection
            )
            connection.execute(
                update(version_table).values(version_num="future_release")
            )
        for allowed in ("", "another_release"):
            with (
                self.subTest(allowed=allowed),
                patch.dict(os.environ, {"DB_COMPATIBLE_REVISIONS": allowed}),
            ):
                engine = get_engine()
                self.addCleanup(engine.dispose)
                with self.assertRaisesRegex(RuntimeError, "schema is not ready"):
                    check_database_ready()
        with patch.dict(os.environ, {"DB_COMPATIBLE_REVISIONS": "future_release"}):
            engine = get_engine()
            self.addCleanup(engine.dispose)
            check_database_ready()
        with get_engine().begin() as connection:
            connection.execute(version_table.insert().values(version_num="second_head"))
        with patch.dict(
            os.environ, {"DB_COMPATIBLE_REVISIONS": "future_release,second_head"}
        ):
            engine = get_engine()
            self.addCleanup(engine.dispose)
            with self.assertRaisesRegex(RuntimeError, "schema is not ready"):
                check_database_ready()

    def test_reviewed_expand_migration_preserves_old_model_reads_and_writes(
        self,
    ) -> None:
        init_database()
        user_id, team_id = self.seed_user_and_team()
        with get_session() as session:
            original = Note(
                owner_id=user_id,
                team_id=team_id,
                title="Before expansion",
                content="Keep the existing content",
            )
            session.add(original)
            session.flush()
            note_id = original.id
        migration_path = Path(__file__).resolve().parents[1] / "app" / "migrations"
        (current_head,) = ScriptDirectory(str(migration_path)).get_heads()
        self.assertEqual(current_head, "006_login_throttle")
        with TemporaryDirectory() as temporary_directory:
            copytree(migration_path, temporary_directory, dirs_exist_ok=True)
            (Path(temporary_directory) / "versions" / "007_expand.py").write_text(
                "from alembic import op\nimport sqlalchemy as sa\n"
                'revision = "007_expand"\n'
                f"down_revision = {current_head!r}\n"
                "branch_labels = None\ndepends_on = None\n\n"
                "def upgrade() -> None:\n"
                '    op.add_column("notes", sa.Column("release_metadata", sa.Text(), nullable=True))\n\n'
                "def downgrade() -> None:\n"
                '    op.drop_column("notes", "release_metadata")\n',
                encoding="utf-8",
            )
            with get_engine().begin() as connection:
                future_config = migration_config(connection)
                future_config.set_main_option("script_location", temporary_directory)
                command.upgrade(future_config, "007_expand")
        with get_engine().begin() as connection:
            expanded_notes = Table("notes", MetaData(), autoload_with=connection)
            connection.execute(
                update(expanded_notes)
                .where(expanded_notes.c.id == note_id)
                .values(release_metadata="Written by the next release")
            )
        with patch.dict(os.environ, {"DB_COMPATIBLE_REVISIONS": ""}):
            with self.assertRaisesRegex(RuntimeError, "schema is not ready"):
                check_database_ready()
        with patch.dict(
            os.environ,
            {"AUTO_MIGRATE": "false", "DB_COMPATIBLE_REVISIONS": "007_expand"},
        ):
            compatible_engine = get_engine()
            self.addCleanup(compatible_engine.dispose)
            check_database_ready()
            # These are the old six-revision ORM models: they do not map the new column.
            with get_session() as session:
                preserved = session.get(Note, note_id)
                assert preserved is not None
                self.assertEqual(
                    (preserved.owner_id, preserved.team_id, preserved.content),
                    (user_id, team_id, "Keep the existing content"),
                )
                preserved.title = "Saved by the old release"
            with get_session() as session:
                old_model = session.get(Note, note_id)
                assert old_model is not None
                self.assertEqual(old_model.title, "Saved by the old release")
                metadata_value = session.scalar(
                    select(expanded_notes.c.release_metadata).where(
                        expanded_notes.c.id == note_id
                    )
                )
                self.assertEqual(metadata_value, "Written by the next release")

    def test_nonowner_runtime_role_can_run_readiness_and_crud_with_documented_grants(
        self,
    ) -> None:
        init_database()
        roles = table(
            "pg_roles",
            column("rolname"),
            column("rolsuper", Boolean),
            column("rolcreaterole", Boolean),
            schema="pg_catalog",
        )
        with self.admin_engine.connect() as connection:
            owner_role = connection.execute(select(func.current_user())).scalar_one()
            database_name = connection.execute(
                select(func.current_database())
            ).scalar_one()
            can_create_roles = connection.execute(
                select(or_(roles.c.rolsuper, roles.c.rolcreaterole)).where(
                    roles.c.rolname == owner_role
                )
            ).scalar_one()
        if not can_create_roles:
            self.skipTest(
                "The dedicated test database role needs CREATEROLE for this check"
            )
        role = "test_runtime_" + uuid4().hex
        password = secrets.token_urlsafe(24)
        quote = self.admin_engine.dialect.identifier_preparer.quote
        quoted_role, quoted_owner = quote(role), quote(owner_role)
        quoted_schema, quoted_database = quote(self.schema), quote(database_name)
        extension_schema = quote(get_settings().db_trgm_schema)
        password_literal = str(
            literal(password).compile(
                dialect=self.admin_engine.dialect,
                compile_kwargs={"literal_binds": True},
            )
        )
        runtime_engine: Engine | None = None

        def remove_role() -> None:
            if runtime_engine is not None:
                runtime_engine.dispose()
            with self.admin_engine.begin() as connection:
                for statement in (
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {quoted_owner} IN SCHEMA {quoted_schema} REVOKE ALL ON TABLES FROM {quoted_role}",
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {quoted_owner} IN SCHEMA {quoted_schema} REVOKE ALL ON SEQUENCES FROM {quoted_role}",
                    f"REVOKE ALL ON ALL TABLES IN SCHEMA {quoted_schema} FROM {quoted_role}",
                    f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {quoted_schema} FROM {quoted_role}",
                    f"REVOKE USAGE ON SCHEMA {quoted_schema}, {extension_schema} FROM {quoted_role}",
                    f"REVOKE CONNECT ON DATABASE {quoted_database} FROM {quoted_role}",
                    f"DROP ROLE {quoted_role}",
                ):
                    connection.execute(DDL(statement))

        with self.admin_engine.begin() as connection:
            # Role/grant DDL is test infrastructure. Identifiers and the generated
            # password are quoted with SQLAlchemy; application requests use ORM queries.
            connection.execute(
                DDL(f"CREATE ROLE {quoted_role} LOGIN PASSWORD {password_literal}")
            )
        self.addCleanup(remove_role)
        domain_tables = ", ".join(
            f"{quoted_schema}.{quote(name)}" for name in sorted(Base.metadata.tables)
        )
        with self.admin_engine.begin() as connection:
            for statement in (
                f"GRANT CONNECT ON DATABASE {quoted_database} TO {quoted_role}",
                f"GRANT USAGE ON SCHEMA {quoted_schema}, {extension_schema} TO {quoted_role}",
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {domain_tables} TO {quoted_role}",
                f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {quoted_schema} TO {quoted_role}",
                f"GRANT SELECT ON TABLE {quoted_schema}.alembic_version TO {quoted_role}",
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {quoted_owner} IN SCHEMA {quoted_schema} GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {quoted_role}",
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {quoted_owner} IN SCHEMA {quoted_schema} GRANT USAGE, SELECT ON SEQUENCES TO {quoted_role}",
            ):
                connection.execute(DDL(statement))
            future_table = Table(
                "runtime_grant_probe",
                MetaData(),
                Column("id", Integer, Identity(), primary_key=True),
                Column("label", Text, nullable=False),
                schema=self.schema,
            )
            future_table.create(connection)
        runtime_url = self.test_url.set(
            username=role, password=password
        ).update_query_dict({"options": f"-csearch_path={self.schema}"})
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": runtime_url.render_as_string(hide_password=False),
                "AUTO_MIGRATE": "false",
            },
        ):
            runtime_engine = get_engine()
            check_database_ready()
            with get_session() as session:
                self.assertEqual(session.scalar(select(func.current_user())), role)
                self.assertFalse(
                    session.scalar(
                        select(func.has_schema_privilege(self.schema, "CREATE"))
                    )
                )
                self.assertFalse(
                    session.scalar(
                        select(func.has_database_privilege(database_name, "CREATE"))
                    )
                )
                self.assertFalse(
                    session.scalar(
                        select(
                            func.has_table_privilege(
                                f"{self.schema}.alembic_version", "UPDATE"
                            )
                        )
                    )
                )
                probe_id = session.execute(
                    future_table.insert()
                    .values(label="Default privileges work")
                    .returning(future_table.c.id)
                ).scalar_one()
                self.assertEqual(
                    session.scalar(select(future_table.c.label)),
                    "Default privileges work",
                )
                session.execute(
                    future_table.delete().where(future_table.c.id == probe_id)
                )
            with TestClient(app) as client:
                self.assertEqual(client.get("/ready").status_code, 200)
                registered = client.post(
                    "/auth/register",
                    json={
                        "email": "runtime@example.com",
                        "display_name": "Runtime",
                        "password": "runtime-role-passphrase",
                    },
                )
                self.assertEqual(registered.status_code, 201, registered.text)
                login = client.post(
                    "/auth/login",
                    json={
                        "email": "runtime@example.com",
                        "password": "runtime-role-passphrase",
                    },
                )
                self.assertEqual(login.status_code, 200, login.text)
                headers = {"Authorization": "Bearer " + login.json()["access_token"]}
                created = client.post(
                    "/notes", json={"title": "Runtime note"}, headers=headers
                )
                self.assertEqual(created.status_code, 201, created.text)
                location = created.headers["Location"]
                fetched = client.get(location, headers=headers)
                self.assertEqual(fetched.status_code, 200, fetched.text)
                updated = client.put(
                    location,
                    headers=headers,
                    json={
                        "title": "Updated runtime note",
                        "content": "Saved with a nonowner role",
                        "version": fetched.json()["version"],
                    },
                )
                self.assertEqual(updated.status_code, 200, updated.text)
                removed = client.delete(
                    location,
                    headers=headers,
                    params={"version": updated.json()["version"]},
                )
                self.assertEqual(removed.status_code, 204, removed.text)

    def test_cleanup_is_batched_and_preserves_active_authentication_data(self) -> None:
        init_database()
        user_id, _ = self.seed_user_and_team()
        now = datetime.now(UTC)
        with get_session() as session:
            for index in range(3):
                session.add(
                    AuthSession(
                        token_hash=f"{index:064x}",
                        user_id=user_id,
                        created_at=now - timedelta(hours=2),
                        expires_at=now - timedelta(hours=1),
                    )
                )
            session.add(
                AuthSession(
                    token_hash="f" * 64,
                    user_id=user_id,
                    expires_at=now + timedelta(hours=1),
                )
            )
            session.add_all(
                [
                    LoginThrottle(
                        subject_hash="1" * 64,
                        window_started_at=now - timedelta(hours=1),
                        attempts=1,
                    ),
                    LoginThrottle(
                        subject_hash="2" * 64, window_started_at=now, attempts=1
                    ),
                ]
            )
        first = cleanup_expired(batch_size=1, max_batches=1)
        self.assertEqual(first, {"sessions_deleted": 1, "throttle_windows_deleted": 1})
        second = cleanup_expired(batch_size=2, max_batches=2)
        self.assertEqual(second, {"sessions_deleted": 2, "throttle_windows_deleted": 0})
        with get_session() as session:
            self.assertEqual(
                list(session.scalars(select(AuthSession.token_hash))), ["f" * 64]
            )
            self.assertEqual(
                list(session.scalars(select(LoginThrottle.subject_hash))), ["2" * 64]
            )
