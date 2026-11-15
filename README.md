# Notes API

A FastAPI REST service for a note-taking product shared across teams.

The foundation includes a health endpoint, typed SQLAlchemy models, pooled
PostgreSQL sessions, and versioned Alembic migrations. The schema has users,
teams, memberships, and notes. Authentication, team permissions, and note APIs
will follow in small, reviewable increments.

## Run locally

Install Docker with Docker Compose, then run from the project directory:

```sh
docker compose up --build -d --wait
curl -i http://127.0.0.1:8000/health
```

The expected response is HTTP `200 OK` with `{"status":"ok"}`. The health endpoint
reports process liveness; it does not query the database. Interactive API docs
are at [localhost:8000/docs](http://127.0.0.1:8000/docs), with the OpenAPI schema at
[localhost:8000/openapi.json](http://127.0.0.1:8000/openapi.json).

The Dockerfile uses Python 3.12 and runs the API as an unprivileged user. Compose
waits for PostgreSQL and the API's HTTP health check to pass. Stop the services
with `docker compose down`; the named database volume is preserved.

## Database

`DATABASE_URL` is required. SQLAlchemy uses PostgreSQL through Psycopg 3. The
engine manages a reusable connection pool; `get_session()` commits successful
transactions, rolls back failures, and returns connections to the pool when each
session closes. Application shutdown disposes the pool, including after a
failed startup.

Compose runs PostgreSQL 18 with database, username, and password `notes`. These
credentials are for local development. The database listens only on
`127.0.0.1:5432`, with data in a named volume mounted at `/var/lib/postgresql`.
Compose provides `postgresql://notes:notes@db:5432/notes` to the API; host processes
use `127.0.0.1` instead of the Compose hostname `db`.

Startup upgrades Alembic Python revisions in one transaction. A PostgreSQL
advisory transaction lock serializes concurrent initializers. `alembic_version`
records the current revision; unknown revisions are rejected without modifying
application data. To apply migrations without starting the server:

```sh
docker compose run --rm api python -c 'from app.database import init_database; init_database()'
```

| Table | Purpose |
| --- | --- |
| `users` | Accounts with case-insensitive unique email addresses |
| `teams` | Named teams |
| `memberships` | Team membership with `owner` or `member` roles |
| `notes` | Notes with a required user owner and an optional team |
| `alembic_version` | Current schema revision |

Foreign keys preserve ownership and membership relationships. PostgreSQL
`timestamptz` stores creation and modification timestamps. Note update operations
must explicitly set `updated_at`. The relational schema alone does not implement
API authorization; authentication, team rules, and note endpoints are planned.

## Verify

Run dependency validation, linting, type checking, and PostgreSQL integration
tests using Docker:

```sh
docker compose --profile test run --build --rm tests
```

No host Python, virtual environment, or manual database setup is needed. The test
image installs HTTPX2, Ruff, and Pyright from `requirements-dev.txt`, then runs
`pip check`, Ruff, Pyright, and the tests. Deprecation warnings fail the tests.
The runtime image excludes test dependencies and test source.

The isolated test database has no published host port and uses temporary
storage. Each test creates and removes a unique schema; development data remains
untouched. Tests cover data persistence, rollback, database constraints, schema
and model consistency, concurrent startup, incremental upgrades, unknown
revisions, and failed-migration atomicity. The startup test exercises the real
application lifespan and health response.

Stop all services with `docker compose --profile test down`; the named
development volume is preserved.

## Optional host development

Requires Python 3.11 or newer and PostgreSQL. If the container API is using port
8000, run `docker compose stop api` before starting the host server:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
docker compose up -d --wait db
export DATABASE_URL='postgresql://notes:notes@127.0.0.1:5432/notes'
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

On Windows, activate `.venv\Scripts\activate` and set environment variables using
your shell's syntax. Select this project's `.venv` in VS Code's **Python: Select
Interpreter** command. Install the recommended Python, Pylance, and Ruff
extensions. The shared `pyproject.toml` targets Python 3.11 and treats deprecated
API use as an error. Context managers use `Generator` or `AsyncGenerator` with
explicit type parameters.

With the environment activated, run static checks with:

```sh
python -m pip check
python -m ruff check app tests
python -m pyright
```

## Project layout

```text
app/
  database.py       # Pooled sessions and transactional migration runner
  main.py           # Application lifespan and health endpoint
  models.py         # Typed SQLAlchemy models
  migrations/       # Alembic Python revisions
tests/
  test_database.py  # Transactions, persistence, and constraints
  test_migrations.py # Schema versioning and upgrade behavior
  test_startup.py   # Application startup and health check
Dockerfile          # Separate runtime and test stages
compose.yaml        # API, PostgreSQL, and isolated test services
requirements.txt    # Runtime dependencies
requirements-dev.txt # Test and quality-check dependencies
pyproject.toml      # Shared Python checks
AGENTS.md           # Project instructions
```

All application source, migrations, and tests are included. A fresh checkout can
be initialized with the commands above without copying database files.

## Commit conventions

Use `type(scope): short description`, with an optional scope and an imperative
subject, for example `feat(db): add note ownership`. Common types are `feat`,
`fix`, `docs`, `test`, `refactor`, and `chore`. Keep commits focused and runnable,
including related tests and documentation. Do not commit databases, virtual
environments, caches, or secrets.

## Continuous integration

GitHub Actions runs the Docker test command, starts the application stack,
checks HTTP endpoints, and verifies that the API container runs as an
unprivileged user. The checkout action is pinned, repository permissions are
read-only, and no repository secrets are required for the local test database.

## Design choices and tradeoffs

1. **Typed SQLAlchemy models and transactional sessions.** ORM mappings express
   the schema and database operations through Python types. A pooled engine and
   short session scopes keep commit and rollback behavior explicit. The tradeoff
   is an ORM dependency and pool configuration that will need tuning as usage
   grows.
2. **Alembic revisions under a database lock.** Reviewed revisions preserve data
   across schema changes. One transaction and an advisory lock make repeated or
   concurrent initialization safe. The tradeoff is migration work during startup
   until releases gain a dedicated migration step.
3. **Isolated PostgreSQL integration tests.** Tests use the real database to
   verify constraints and rollback. Temporary per-test schemas protect local
   data. Separate container stages keep test tools out of the API image, at the
   cost of Docker build and database startup time.

## What I would change, add, or stop with more time

- **Change:** Tune connection pools and indexes using measured workloads.
- **Add:** Authentication, team authorization, and note CRUD in focused increments.
- **Add:** Note revision history, sharing audit logs, and backup/restore checks.
- **Stop:** Applying migrations at every application startup once a coordinated
  release process can run them as an explicit deployment step.
