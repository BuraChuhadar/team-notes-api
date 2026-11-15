# Notes API

A FastAPI REST service for a note-taking product shared across teams.

This first increment provides a health endpoint and a PostgreSQL schema managed
with SQLAlchemy 2.1 and Alembic. Authentication, team management, and note endpoints
are planned increments; they are not exposed yet.

## Run locally

Use Python 3.11 or newer and a running PostgreSQL server. Provision separate
`notes` and `notes_test` databases and a role that can create tables and schemas.
From the project directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
export DATABASE_URL='postgresql://notes:notes@127.0.0.1:5432/notes'
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

The example credentials are for local development. Replace the connection URL to
match your server. `DATABASE_URL` is required; the application does not load `.env`
files automatically. On Windows, activate `.venv\Scripts\activate` and set the
variable using your shell's syntax. Stop the server with Ctrl+C.

```sh
curl -i http://127.0.0.1:8000/health
```

Expected: HTTP `200 OK` with `{"status":"ok"}`. This endpoint reports process
liveness; it does not query the database. Interactive documentation is available
at `/docs`, and the OpenAPI schema at `/openapi.json`.

## Database

Typed SQLAlchemy models describe users, teams, memberships, and notes. Notes have
a required user owner and an optional team. Database constraints enforce valid
references, nonblank text, membership roles, and case-insensitive unique email
addresses. Timestamps use PostgreSQL `timestamptz`.

`get_session()` creates a short-lived unit of work, commits success, rolls back
failure, and returns the connection to the pool when it closes. Application code
uses ORM models and SQLAlchemy expressions with bound values.

Startup applies Alembic Python revisions in one transaction. A PostgreSQL
advisory lock serializes concurrent initializations; `alembic_version` records
the current revision. Repeating initialization preserves existing data, and
unknown revisions are rejected. Apply migrations directly with:

```sh
python -c 'from app.database import init_database; init_database()'
```

## Verify

Use a dedicated test database; never set `TEST_DATABASE_URL` to your development
database. Tests create and remove unique schemas inside the test database:

```sh
export TEST_DATABASE_URL='postgresql://notes:notes@127.0.0.1:5432/notes_test'
python -m pip check
python -m ruff check app tests
python -m pyright
python -W error::DeprecationWarning -W error::PendingDeprecationWarning -m unittest discover -s tests -v
```

The tests check required configuration, persistence across sessions and repeat
initialization, rollback and connection cleanup, and application startup with a
working health endpoint. PostgreSQL tests are skipped when `TEST_DATABASE_URL`
is absent.

In VS Code, install the recommended Python, Pylance, and Ruff extensions and
select this project's `.venv` interpreter. Shared `pyproject.toml` settings target
Python 3.11 and treat deprecated APIs as errors. Context managers use explicit
`Generator` and `AsyncGenerator` annotations compatible with Python 3.11 and 3.12.

## Project layout

```text
app/
  database.py       # Pooled sessions and Alembic migration runner
  main.py           # Application lifecycle and health endpoint
  models.py         # Typed SQLAlchemy schema
  migrations/       # Alembic Python revisions
tests/
  test_database.py  # Configuration, transaction, and startup behavior
requirements.txt    # Runtime dependencies
requirements-dev.txt # Development dependencies
pyproject.toml      # Shared type and lint checks
AGENTS.md           # Development guidance
```
