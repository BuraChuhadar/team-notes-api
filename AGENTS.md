# Project guidance

## Working style

- Develop in small, reviewable steps limited to the current request.
- Leave changes unstaged and uncommitted unless the user explicitly asks otherwise.
- Keep the implementation simple. Add dependencies only when the feature needs them.
- Update README.md when setup, configuration, or behavior changes.

## Commit conventions

- Use Conventional Commits: `type(scope): short description`; the scope is optional.
- Use `feat`, `fix`, `docs`, `test`, `refactor`, or `chore` as appropriate.
- Use an imperative subject around 72 characters or fewer.
- Keep each commit focused and runnable, with related tests and documentation.
- Never commit database files, virtual environments, caches, or secrets.

## Project

- Python 3.11+, FastAPI, SQLAlchemy 2.1, Alembic, PostgreSQL, and Psycopg 3.
- Entry point: `app.main:app`. The health endpoint is implemented.
- Sessions: `app/database.py`; typed models: `app/models.py`;
  migrations: `app/migrations/versions/`.
- `DATABASE_URL` is required. Use PostgreSQL; do not add automatic `.env` loading.
- The schema includes users, teams, memberships, and notes. Authentication,
  authorization, and note APIs are future increments.
- Docker Compose supplies local PostgreSQL. Its named volume is outside Git.
- The Dockerfile separates runtime and test dependencies and uses an unprivileged
  API user.

## Commands

Run from the repository root:

```sh
docker compose up --build -d --wait
curl -i http://127.0.0.1:8000/health
docker compose --profile test run --build --rm tests
```

For host development, activate the virtual environment, install
`requirements-dev.txt`, and provide `DATABASE_URL`. Stop the container API first
if it occupies port 8000. README.md contains complete setup commands.

The test container runs dependency validation, Ruff, Pyright, and integration
tests. With the host virtual environment activated, run `python -m pip check`,
`python -m ruff check app tests`, and `python -m pyright`. Keep the Python 3.11
target and deprecated-API errors in `pyproject.toml`; fix warnings rather than
suppressing them.

## Database changes

- Keep initialization repeatable and preserve existing data.
- Use typed SQLAlchemy ORM models and SQLAlchemy expressions with bound values.
- Use short session scopes through `get_session()`. Commit successful work,
  roll back failures, and return connections to the pool when sessions close.
- Add explicit Alembic Python revisions for schema changes. Do not edit applied
  revisions; preserve transactional upgrades and advisory locking.
- Reject unknown revisions instead of changing a newer database.
- Use `TEST_DATABASE_URL` and unique test schemas. Never change development or
  public-schema data in tests. The Compose test database has no published port.

## Verification

Run checks relevant to the change. Database changes should verify persistence,
constraints, and rollback. Migration changes should cover repeat and concurrent
initialization, incremental data preservation, and failed-upgrade atomicity.
Startup changes should verify the lifespan and health endpoint. Report verified
behavior and any remaining limits.

## Project requirements

- Include all application source, migrations, tests, and reproducible commands.
- Document two or three design decisions, their reasons, and tradeoffs.
- Explain what would be changed, added, or stopped with more time.
- Keep those sections aligned with implemented behavior and distinguish planned
  features from completed work.
