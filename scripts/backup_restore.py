"""Back up PostgreSQL in custom format and verify a separate recovery database.

Read connection URLs from named environment variables. Native pg_dump/pg_restore
support cloud operation; --db-container uses tools in a local PostgreSQL container.
No connection URL or password is included in tool command arguments or reports.
"""

import argparse
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import MetaData, Table, create_engine, func, inspect, select
from sqlalchemy.engine import Connection, URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.pool import NullPool

PG_QUERY_ENV = {
    "sslmode": "PGSSLMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY",
    "sslpassword": "PGSSLPASSWORD",
    "sslcrl": "PGSSLCRL",
    "sslcrldir": "PGSSLCRLDIR",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "application_name": "PGAPPNAME",
    "options": "PGOPTIONS",
    "target_session_attrs": "PGTARGETSESSIONATTRS",
    "channel_binding": "PGCHANNELBINDING",
    "gssencmode": "PGGSSENCMODE",
    "load_balance_hosts": "PGLOADBALANCEHOSTS",
}
PG_CONNECTION_ENV = {
    "PGHOST",
    "PGPORT",
    "PGUSER",
    "PGPASSWORD",
    "PGDATABASE",
    *PG_QUERY_ENV.values(),
}
# libpq settings can also be baked into the container image. Remove defaults
# absent from the validated URL without putting any credential into argv.
# https://www.postgresql.org/docs/18/libpq-envars.html
PG_CONTAINER_DEFAULTS = PG_CONNECTION_ENV | {
    "PGHOSTADDR",
    "PGSERVICE",
    "PGSERVICEFILE",
    "PGPASSFILE",
    "PGSYSCONFDIR",
    "PGSSLNEGOTIATION",
    "PGREQUIREAUTH",
    "PGREQUIRESSL",
    "PGSSLCOMPRESSION",
    "PGSSLCERTMODE",
    "PGSSLSNI",
    "PGREQUIREPEER",
    "PGSSLMINPROTOCOLVERSION",
    "PGSSLMAXPROTOCOLVERSION",
    "PGKRBSRVNAME",
    "PGGSSLIB",
    "PGGSSDELEGATION",
    "PGCLIENTENCODING",
    "PGMINPROTOCOLVERSION",
    "PGMAXPROTOCOLVERSION",
    "PGDATESTYLE",
    "PGTZ",
    "PGGEQO",
}


def database_url(variable: str) -> URL:
    """Parse a PostgreSQL URL without reflecting invalid input in exceptions."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
        raise ValueError(
            "Connection environment variable names must be valid identifiers."
        )
    value = os.environ.get(variable)
    if not value:
        raise ValueError(f"Set {variable} to the database URL.")
    try:
        url = make_url(value)
    except (ArgumentError, ValueError):
        raise ValueError(
            f"{variable} must contain a valid PostgreSQL connection URL."
        ) from None
    return _validated_url(url)


def _validated_url(url: URL) -> URL:
    """Keep SQLAlchemy and libpq on the same explicitly selected endpoint."""
    if url.drivername not in {"postgresql", "postgres", "postgresql+psycopg"}:
        raise ValueError("A PostgreSQL database URL is required.")
    if not url.database or not url.username or not url.host:
        raise ValueError(
            "The database URL must specify a host, database, and username."
        )
    if "=" in url.database or url.database.startswith(("postgresql://", "postgres://")):
        raise ValueError("Use a plain database name, not a connection string.")
    if set(url.query) - PG_QUERY_ENV.keys():
        raise ValueError(
            "Unsupported database URL option; routing and credentials must be in the URL fields."
        )
    if any(not isinstance(value, str) for value in url.query.values()):
        raise ValueError(
            "Repeated connection options are not supported by the backup tool."
        )
    return url.set(drivername="postgresql+psycopg")


def _clear_libpq_environment() -> None:
    """This standalone command selects connections only through validated URLs."""
    for name in list(os.environ):
        if name.startswith("PG"):
            del os.environ[name]


def tool_environment(url: URL, db_container: str | None = None) -> dict[str, str]:
    """Configure libpq through individual environment values, never a URL argument."""
    url = _validated_url(url)
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("PG")
    }
    values = {
        "PGHOST": url.host,
        "PGPORT": str(url.port or 5432),
        "PGUSER": url.username,
        "PGPASSWORD": url.password,
        "PGDATABASE": url.database,
        "PGCONNECT_TIMEOUT": "5",
        "PGAPPNAME": "notes-backup-restore",
    }
    if db_container is not None:
        values["PGHOST"] = "127.0.0.1"
        values["PGPORT"] = "5432"
    for query_name, environment_name in PG_QUERY_ENV.items():
        value = url.query.get(query_name)
        if isinstance(value, tuple):
            raise ValueError(
                "Repeated connection options are not supported by the backup tool."
            )
        if value is not None:
            values[environment_name] = value
    environment.update(
        {name: value for name, value in values.items() if value is not None}
    )
    return environment


def tool_command(
    tool: str, db_container: str | None, environment: dict[str, str]
) -> list[str]:
    if db_container is not None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", db_container):
            raise ValueError("Supply a valid PostgreSQL container name or ID.")
        if not shutil.which("docker"):
            raise ValueError("Install Docker to use --db-container.")
        command = ["docker", "exec", "--interactive"]
        for name in sorted(PG_CONNECTION_ENV & environment.keys()):
            command.extend(["--env", name])
        unset = [
            argument
            for name in sorted(PG_CONTAINER_DEFAULTS - environment.keys())
            for argument in ("-u", name)
        ]
        return [*command, db_container, "env", *unset, tool]
    if not shutil.which(tool):
        raise ValueError(
            f"Install PostgreSQL 18 client tools or supply --db-container to run {tool}."
        )
    return [tool]


def _application_objects(connection: Connection) -> list[tuple[str, str]]:
    inspector = inspect(connection)
    objects: list[tuple[str, str]] = []
    for schema in inspector.get_schema_names():
        if schema in {"pg_catalog", "information_schema"} or schema.startswith("pg_"):
            continue
        for getter in (
            inspector.get_table_names,
            inspector.get_view_names,
            inspector.get_materialized_view_names,
            inspector.get_sequence_names,
        ):
            objects.extend((schema, name) for name in getter(schema=schema))
    return sorted(set(objects))


def application_tables(url: URL) -> list[tuple[str, str]]:
    url = _validated_url(url)
    _clear_libpq_environment()
    engine = create_engine(url, connect_args={"connect_timeout": 5}, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            return sorted(
                (schema, name)
                for schema in inspector.get_schema_names()
                if schema not in {"pg_catalog", "information_schema"}
                and not schema.startswith("pg_")
                for name in inspector.get_table_names(schema=schema)
            )
    finally:
        engine.dispose()


def _database_identity(url: URL) -> tuple[str | None, int, str | None]:
    return (url.host, url.port or 5432, url.database)


def _verified_container(url: URL, db_container: str, timeout_seconds: int) -> str:
    """Prove the URL and pinned container reach the same PostgreSQL database."""
    host = url.host or ""
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise ValueError("--db-container requires a verified loopback database URL.")
    # Validate the identifier before giving it to Docker; inspect only nonsecret fields.
    environment = tool_environment(url, db_container)
    tool_command("psql", db_container, environment)
    inspected = subprocess.run(
        [
            "docker",
            "inspect",
            "--type",
            "container",
            "--format",
            "{{.Id}} {{.State.Running}}",
            db_container,
        ],
        env=environment,
        capture_output=True,
        timeout=timeout_seconds,
    )
    match = re.fullmatch(rb"([0-9a-f]{64}) true\s*", inspected.stdout or b"")
    if inspected.returncode or match is None:
        raise ValueError("Cannot verify the running PostgreSQL container.")
    container_id = match.group(1).decode("ascii")
    command = tool_command("psql", container_id, environment)
    _clear_libpq_environment()
    engine = create_engine(url, connect_args={"connect_timeout": 5}, poolclass=NullPool)
    try:
        with engine.begin() as connection:
            # Transaction advisory locks are scoped to this database. A second
            # backend must observe this nonce as already held, proving identity
            # even with remote Docker contexts or misleading published ports.
            nonce = secrets.randbits(63)
            connection.execute(select(func.pg_advisory_xact_lock(nonce)))
            probe = select(func.pg_try_advisory_xact_lock(nonce)).compile(
                dialect=connection.dialect, compile_kwargs={"literal_binds": True}
            )
            checked = subprocess.run(
                [
                    *command,
                    "--no-psqlrc",
                    "--tuples-only",
                    "--no-align",
                    "--set=ON_ERROR_STOP=1",
                    "--command",
                    str(probe),
                ],
                env=environment,
                capture_output=True,
                timeout=timeout_seconds,
            )
            if checked.returncode or checked.stdout.strip() != b"f":
                raise ValueError(
                    "The URL and PostgreSQL container do not identify the same database."
                )
    finally:
        engine.dispose()
    return container_id


def dump_database(
    url: URL, output: Path, db_container: str | None = None, timeout_seconds: int = 300
) -> dict[str, Any]:
    url = _validated_url(url)
    if output.suffix != ".dump":
        raise ValueError("Use a .dump filename for a custom-format backup.")
    if db_container is not None:
        db_container = _verified_container(url, db_container, timeout_seconds)
    environment = tool_environment(url, db_container)
    command = tool_command("pg_dump", db_container, environment)
    output.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as archive:
            result = subprocess.run(
                [*command, "--format=custom", "--no-owner", "--no-privileges"],
                env=environment,
                stdout=archive,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
            )
        if result.returncode:
            raise RuntimeError(
                "pg_dump failed; check connectivity, PostgreSQL versions, and backup-role permissions."
            )
        digest = hashlib.sha256()
        with output.open("rb") as archive:
            for chunk in iter(lambda: archive.read(1024 * 1024), b""):
                digest.update(chunk)
        return {
            "archive": str(output),
            "bytes": output.stat().st_size,
            "sha256": digest.hexdigest(),
        }
    except Exception:
        if created:
            output.unlink(missing_ok=True)
        raise


def restore_database(
    url: URL,
    archive: Path,
    db_container: str | None = None,
    *,
    source_url: URL,
    timeout_seconds: int = 300,
) -> None:
    """Restore into a separate empty database with transactional pg_restore."""
    url = _validated_url(url)
    source_url = _validated_url(source_url)
    if _database_identity(url) == _database_identity(source_url):
        raise ValueError("Restore target must be separate from the source database.")
    if db_container is not None:
        db_container = _verified_container(url, db_container, timeout_seconds)
    _clear_libpq_environment()
    engine = create_engine(url, connect_args={"connect_timeout": 5}, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            if _application_objects(connection):
                raise ValueError(
                    "Restore target must be an empty, separately provisioned database."
                )
    finally:
        engine.dispose()
    environment = tool_environment(url, db_container)
    command = tool_command("pg_restore", db_container, environment)
    # --dbname receives only the database name; credentials stay in PG* env.
    with archive.open("rb") as source:
        result = subprocess.run(
            [
                *command,
                "--format=custom",
                "--single-transaction",
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                "--dbname",
                url.database or "",
            ],
            env=environment,
            stdin=source,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
    if result.returncode:
        raise RuntimeError(
            "pg_restore failed; inspect the empty target before retrying and check PostgreSQL versions and restore-role permissions."
        )


def _canonical_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return (
            value.astimezone(UTC).isoformat()
            if value.tzinfo is not None
            else value.isoformat()
        )
    if isinstance(value, date | time):
        return value.isoformat()
    if isinstance(value, bytes | bytearray | memoryview):
        return {"$bytes": bytes(value).hex()}
    if isinstance(value, Decimal | UUID):
        return str(value)
    raise TypeError("Unsupported value encountered while comparing the databases.")


def fingerprint(url: URL) -> dict[str, dict[str, Any]]:
    """Compare quiesced datasets without emitting rows or retaining full row bodies."""
    url = _validated_url(url)
    _clear_libpq_environment()
    engine = create_engine(url, connect_args={"connect_timeout": 5}, poolclass=NullPool)
    result: dict[str, dict[str, Any]] = {}
    try:
        with engine.connect().execution_options(
            isolation_level="REPEATABLE READ"
        ) as connection:
            inspector = inspect(connection)
            tables = sorted(
                (schema, name)
                for schema in inspector.get_schema_names()
                if schema not in {"pg_catalog", "information_schema"}
                and not schema.startswith("pg_")
                for name in inspector.get_table_names(schema=schema)
            )
            for schema, name in tables:
                table = Table(name, MetaData(), schema=schema, autoload_with=connection)
                rows = (
                    connection.execution_options(stream_results=True)
                    .execute(select(table))
                    .mappings()
                )
                row_hashes = [
                    hashlib.sha256(
                        json.dumps(
                            dict(row),
                            sort_keys=True,
                            default=_canonical_value,
                            separators=(",", ":"),
                            allow_nan=False,
                        ).encode()
                    ).digest()
                    for row in rows
                ]
                row_hashes.sort()
                digest = hashlib.sha256()
                for row_hash in row_hashes:
                    digest.update(row_hash)
                result[f"{schema}.{name}"] = {
                    "rows": len(row_hashes),
                    "sha256": digest.hexdigest(),
                }
        return result
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["dump", "restore", "compare"])
    parser.add_argument("--url-env", default="DATABASE_URL")
    parser.add_argument("--target-url-env", default="RESTORE_DATABASE_URL")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--db-container")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    args = parser.parse_args()
    try:
        if not 1 <= args.timeout_seconds <= 86_400:
            raise ValueError("--timeout-seconds must be between 1 and 86400.")
        source_url = database_url(args.url_env)
        target_url = (
            database_url(args.target_url_env) if args.action != "dump" else None
        )
        # Parse both URLs first: callers may intentionally name a PG-prefixed
        # environment variable. Inherited libpq settings must affect neither
        # SQLAlchemy's guard queries nor the PostgreSQL client tools.
        _clear_libpq_environment()
        if args.action == "dump":
            if args.archive is None:
                raise ValueError("--archive is required.")
            report = dump_database(
                source_url,
                args.archive,
                args.db_container,
                args.timeout_seconds,
            )
        elif args.action == "restore":
            if args.archive is None:
                raise ValueError("--archive is required.")
            assert target_url is not None
            restore_database(
                target_url,
                args.archive,
                args.db_container,
                source_url=source_url,
                timeout_seconds=args.timeout_seconds,
            )
            report = {
                "restored": True,
                "target": "empty separate database selected by environment variable",
            }
        else:
            assert target_url is not None
            source = fingerprint(source_url)
            target = fingerprint(target_url)
            if source != target:
                raise RuntimeError(
                    "Restored row counts or fingerprints do not match the quiesced source dataset."
                )
            report = {"matches": True, "tables": target}
        print(json.dumps(report, indent=2))
        return 0
    except subprocess.TimeoutExpired:
        print(
            json.dumps(
                {
                    "error": "PostgreSQL tool timed out; inspect the operation outcome before retrying."
                }
            )
        )
    except FileExistsError:
        print(
            json.dumps(
                {"error": "The backup output already exists; choose a new filename."}
            )
        )
    except SQLAlchemyError:
        print(
            json.dumps(
                {
                    "error": "Database operation failed; check connectivity and permissions."
                }
            )
        )
    except (ValueError, RuntimeError) as error:
        print(json.dumps({"error": str(error)}))
    except (OSError, TypeError):
        print(
            json.dumps(
                {
                    "error": "Backup/restore failed; check tool availability, file permissions, and database versions."
                }
            )
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
