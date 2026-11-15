"""Protect backup credentials, private archives, and restore boundaries."""

import hashlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError

from scripts import backup_restore


class BackupToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source_url = URL.create(
            "postgresql+psycopg",
            username="backup_user",
            password="private:test-password",
            host="source.example",
            port=5432,
            database="source",
            query={"sslmode": "verify-full", "sslrootcert": "/safe/root.crt"},
        )
        self.target_url = self.source_url.set(
            host="target.example", database="recovery"
        )
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def test_credentials_stay_in_libpq_environment_not_tool_arguments(self) -> None:
        environment = backup_restore.tool_environment(self.target_url, "test-postgres")
        with patch("shutil.which", return_value="docker"):
            command = backup_restore.tool_command(
                "pg_restore", "test-postgres", environment
            )
        self.assertEqual(environment["PGHOST"], "127.0.0.1")
        self.assertEqual(environment["PGPORT"], "5432")
        self.assertEqual(environment["PGDATABASE"], "recovery")
        self.assertEqual(environment["PGPASSWORD"], "private:test-password")
        self.assertEqual(environment["PGSSLMODE"], "verify-full")
        self.assertNotIn("private:test-password", " ".join(command))
        self.assertNotIn("postgresql://", " ".join(command))
        self.assertIn("PGPASSWORD", command)
        for name in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGPASSFILE"):
            self.assertEqual(command[command.index(name) - 1], "-u")

    def test_native_connection_replaces_ambient_database_credentials(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PGDATABASE": "wrong",
                "PGUSER": "wrong",
                "PGHOST": "wrong",
                "PGPASSWORD": "wrong",
                "PGHOSTADDR": "192.0.2.30",
                "PGSERVICE": "wrong",
                "PGSERVICEFILE": "/wrong/service.conf",
                "PGPASSFILE": "/wrong/passwords",
            },
        ):
            environment = backup_restore.tool_environment(
                URL.create(
                    "postgresql+psycopg",
                    username="backup_user",
                    host="source.example",
                    database="source",
                    query={"sslmode": "verify-full", "sslrootcert": "/safe/root.crt"},
                )
            )
        self.assertEqual(environment["PGDATABASE"], "source")
        self.assertEqual(environment["PGUSER"], "backup_user")
        self.assertEqual(environment["PGHOST"], "source.example")
        self.assertEqual(environment["PGSSLROOTCERT"], "/safe/root.crt")
        self.assertNotIn("PGPASSWORD", environment)
        for name in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGPASSFILE"):
            self.assertNotIn(name, environment)

    def test_routing_query_options_are_refused_before_preflight_or_tools(self) -> None:
        for name in (
            "host",
            "port",
            "dbname",
            "user",
            "password",
            "service",
            "hostaddr",
            "passfile",
            "servicefile",
            "unknown_option",
        ):
            with self.subTest(name=name):
                url = self.target_url.update_query_dict({name: "private-override"})
                with (
                    patch("scripts.backup_restore.create_engine") as engine,
                    patch("subprocess.run") as run,
                ):
                    with self.assertRaisesRegex(ValueError, "Unsupported"):
                        backup_restore.restore_database(
                            url,
                            self.directory / "source.dump",
                            source_url=self.source_url,
                        )
                engine.assert_not_called()
                run.assert_not_called()
                with patch.dict(
                    os.environ,
                    {"BACKUP_TEST_URL": url.render_as_string(hide_password=False)},
                ):
                    with self.assertRaises(ValueError) as failure:
                        backup_restore.database_url("BACKUP_TEST_URL")
                self.assertNotIn("private-override", str(failure.exception))

    def test_container_requires_loopback_url_before_touching_the_database(self) -> None:
        with (
            patch("scripts.backup_restore.create_engine") as engine,
            patch("subprocess.run") as run,
        ):
            with self.assertRaisesRegex(ValueError, "loopback"):
                backup_restore.dump_database(
                    self.source_url, self.directory / "source.dump", "postgres"
                )
        engine.assert_not_called()
        run.assert_not_called()

    def test_container_identity_probe_pins_id_and_refuses_a_different_database(
        self,
    ) -> None:
        container_id = "a" * 64
        for matches in (False, True):
            with self.subTest(matches=matches):
                with (
                    patch("shutil.which", return_value="docker"),
                    patch("scripts.backup_restore.create_engine") as engine,
                    patch(
                        "subprocess.run",
                        side_effect=[
                            subprocess.CompletedProcess(
                                [], 0, stdout=f"{container_id} true\n".encode()
                            ),
                            subprocess.CompletedProcess(
                                [], 0, stdout=b"f\n" if matches else b"t\n"
                            ),
                        ],
                    ) as run,
                ):
                    connection = (
                        engine.return_value.begin.return_value.__enter__.return_value
                    )
                    connection.dialect = postgresql.dialect()
                    if matches:
                        self.assertEqual(
                            backup_restore._verified_container(
                                self.target_url.set(host="127.0.0.1", port=6543),
                                "postgres",
                                5,
                            ),
                            container_id,
                        )
                    else:
                        with self.assertRaisesRegex(ValueError, "same database"):
                            backup_restore._verified_container(
                                self.target_url.set(host="127.0.0.1", port=6543),
                                "postgres",
                                5,
                            )
                    connection.execute.assert_called_once()
                    command = run.call_args.args[0]
                    self.assertIn(container_id, command)
                    self.assertIn("pg_try_advisory_xact_lock", command[-1])
                    self.assertNotIn("private:test-password", " ".join(command))

    def test_preflight_discards_all_inherited_libpq_settings(self) -> None:
        archive = self.directory / "source.dump"
        archive.write_bytes(b"PGDMP-test")
        inherited = {
            name: "wrong"
            for name in (
                "PGHOSTADDR",
                "PGSERVICE",
                "PGSERVICEFILE",
                "PGPASSFILE",
                "PGOPTIONS",
            )
        }
        with (
            patch.dict(os.environ, inherited),
            patch("scripts.backup_restore.create_engine") as engine,
            patch("scripts.backup_restore._application_objects", return_value=[]),
            patch("shutil.which", return_value="pg_restore"),
            patch(
                "subprocess.run", return_value=subprocess.CompletedProcess([], 0)
            ) as run,
        ):

            def guard(*args, **kwargs):
                self.assertFalse(any(name.startswith("PG") for name in os.environ))
                return engine.return_value

            engine.side_effect = guard
            backup_restore.restore_database(
                self.target_url, archive, source_url=self.source_url
            )
            environment = run.call_args.kwargs["env"]
            for name in inherited:
                self.assertNotIn(name, environment)

    def test_cli_parses_pg_prefixed_urls_before_clearing_libpq_environment(
        self,
    ) -> None:
        environment = {
            "PG_SOURCE_URL": self.source_url.render_as_string(hide_password=False),
            "PG_TARGET_URL": self.target_url.render_as_string(hide_password=False),
            "PGHOSTADDR": "192.0.2.30",
        }
        with (
            patch.dict(os.environ, environment),
            patch(
                "sys.argv",
                [
                    "backup_restore.py",
                    "compare",
                    "--url-env",
                    "PG_SOURCE_URL",
                    "--target-url-env",
                    "PG_TARGET_URL",
                ],
            ),
            patch("scripts.backup_restore.fingerprint", return_value={}) as fingerprint,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(backup_restore.main(), 0)
            self.assertNotIn("PGHOSTADDR", os.environ)
        self.assertEqual(fingerprint.call_count, 2)

    def test_invalid_connection_errors_do_not_echo_connection_secrets(self) -> None:
        for value in (
            "postgresql://reader:private-password@host:secret-port/db",
            "postgresql://private-password",
        ):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"BACKUP_TEST_URL": value}):
                    with self.assertRaises(ValueError) as failure:
                        backup_restore.database_url("BACKUP_TEST_URL")
                self.assertNotIn("private-password", str(failure.exception))
                self.assertNotIn("secret-port", str(failure.exception))
                self.assertNotIn("postgresql://", str(failure.exception))

    def test_dump_is_private_while_writing_and_never_overwrites_existing_file(
        self,
    ) -> None:
        archive = self.directory / "private.dump"

        def dump(*args, **kwargs):
            output = kwargs["stdout"]
            self.assertEqual(stat.S_IMODE(os.fstat(output.fileno()).st_mode), 0o600)
            output.write(b"PGDMP-test-archive")
            return subprocess.CompletedProcess(args[0], 0)

        with (
            patch("shutil.which", return_value="pg_dump"),
            patch("subprocess.run", side_effect=dump),
        ):
            report = backup_restore.dump_database(self.source_url, archive)
        self.assertEqual(
            report["sha256"], hashlib.sha256(archive.read_bytes()).hexdigest()
        )
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        before = archive.read_bytes()
        with (
            patch("shutil.which", return_value="pg_dump"),
            patch("subprocess.run") as run,
        ):
            with self.assertRaises(FileExistsError):
                backup_restore.dump_database(self.source_url, archive)
        run.assert_not_called()
        self.assertEqual(archive.read_bytes(), before)
        # Existing empty files are also retained when exclusive creation fails.
        empty = self.directory / "empty.dump"
        empty.touch()
        with patch("shutil.which", return_value="pg_dump"), patch("subprocess.run"):
            with self.assertRaises(FileExistsError):
                backup_restore.dump_database(self.source_url, empty)
        self.assertTrue(empty.exists())

    def test_failed_dump_removes_only_its_own_partial_archive(self) -> None:
        archive = self.directory / "partial.dump"
        with (
            patch("shutil.which", return_value="pg_dump"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess(
                    ["pg_dump"], 1, stderr=b"sensitive tool details"
                ),
            ),
        ):
            with self.assertRaises(RuntimeError) as failure:
                backup_restore.dump_database(self.source_url, archive)
        self.assertFalse(archive.exists())
        self.assertNotIn("sensitive tool details", str(failure.exception))

    def test_restore_refuses_same_or_nonempty_database_without_running_pg_restore(
        self,
    ) -> None:
        archive = self.directory / "source.dump"
        archive.write_bytes(b"PGDMP-test")
        with patch("subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "separate"):
                backup_restore.restore_database(
                    self.source_url, archive, source_url=self.source_url
                )
        run.assert_not_called()
        with (
            patch("scripts.backup_restore.create_engine"),
            patch(
                "scripts.backup_restore._application_objects",
                return_value=[("public", "notes")],
            ),
            patch("subprocess.run") as run,
        ):
            with self.assertRaisesRegex(ValueError, "empty"):
                backup_restore.restore_database(
                    self.target_url, archive, source_url=self.source_url
                )
        run.assert_not_called()

    def test_restore_uses_atomic_flags_and_only_database_name_as_argument(self) -> None:
        archive = self.directory / "source.dump"
        archive.write_bytes(b"PGDMP-test")
        with (
            patch("scripts.backup_restore.create_engine"),
            patch("scripts.backup_restore._application_objects", return_value=[]),
            patch("shutil.which", return_value="pg_restore"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess(["pg_restore"], 0),
            ) as run,
        ):
            backup_restore.restore_database(
                self.target_url, archive, source_url=self.source_url
            )
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--dbname") + 1], "recovery")
        self.assertIn("--single-transaction", command)
        self.assertIn("--exit-on-error", command)
        self.assertNotIn("private:test-password", " ".join(command))
        self.assertNotIn("postgresql", " ".join(command))
        self.assertEqual(
            run.call_args.kwargs["env"]["PGPASSWORD"], "private:test-password"
        )

    def test_cli_redacts_backend_connection_errors(self) -> None:
        output = io.StringIO()
        environment = {
            "DATABASE_URL": self.source_url.render_as_string(hide_password=False),
            "RESTORE_DATABASE_URL": self.target_url.render_as_string(
                hide_password=False
            ),
        }
        with (
            patch.dict(os.environ, environment),
            patch("sys.argv", ["backup_restore.py", "compare"]),
            patch(
                "scripts.backup_restore.fingerprint",
                side_effect=SQLAlchemyError("private:test-password connection details"),
            ),
            redirect_stdout(output),
        ):
            code = backup_restore.main()
        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(output.getvalue()),
            {"error": "Database operation failed; check connectivity and permissions."},
        )
        self.assertNotIn("private:test-password", output.getvalue())
        self.assertNotIn("postgresql", output.getvalue())
