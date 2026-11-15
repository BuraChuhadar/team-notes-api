# Project guidance

## Working style

- Develop in small, reviewable steps limited to the current request.
- Leave changes unstaged and uncommitted unless the user explicitly asks otherwise.
- Keep the implementation simple and update README.md when setup or behavior changes.
- Keep local database files, virtual environments, caches, and secrets out of Git.

## Project

- Python 3.11+, FastAPI, SQLAlchemy 2.1, Alembic, PostgreSQL, and Psycopg 3.
- Application entry point: `app.main:app`.
- Typed models: `app/models.py`; sessions and migrations: `app/database.py`.
- `DATABASE_URL` is required. Do not add a fallback database or automatic `.env` loading.
- The schema includes users, teams, memberships, and notes. Only `/health` is exposed.
- Authentication, team permissions, and note endpoints are future increments.

## Commands

After activating the virtual environment and configuring a local PostgreSQL URL:

```sh
python -m pip install -r requirements-dev.txt
python -m uvicorn app.main:app --reload
python -m pip check
python -m ruff check app tests
python -m pyright
python -W error::DeprecationWarning -W error::PendingDeprecationWarning -m unittest discover -s tests -v
```

Set `TEST_DATABASE_URL` to a separate test database to run PostgreSQL tests.
Tests use unique schemas and must not modify development or public-schema data.

## Database changes

- Use typed SQLAlchemy models and expressions with bound values; do not add handwritten SQL.
- Use `get_session()` for transactions. Commit success, roll back failure, and close sessions.
- Keep initialization repeatable and preserve existing data.
- Add Alembic Python revisions for schema changes; do not edit applied revisions.
- Preserve transactional migrations, advisory locking, and revision checks.

## Verification

Run checks relevant to the change. Persistence changes should verify commit,
rollback, and connection cleanup. Startup changes should verify initialization
and the health endpoint. Keep the shared Python 3.11 target and deprecated-API
errors; fix warnings rather than suppressing them. Report checks and remaining limits.
