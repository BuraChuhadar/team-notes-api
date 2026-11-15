# Project guidance

## Working style

- Develop in small, reviewable steps limited to the current request.
- Leave changes unstaged and uncommitted unless the user explicitly asks otherwise.
- Keep the implementation simple. Add dependencies only when the feature needs them.
- Update README.md when setup, configuration, or behavior changes.

## Commit conventions

- Use Conventional Commits: `type(scope): short description`; the scope is optional.
- Use `feat`, `fix`, `docs`, `test`, `refactor`, or `chore` as appropriate.
- Write the subject in the imperative, such as `feat(db): add note ownership`.
- Keep subjects around 72 characters or fewer.
- Keep each commit focused and runnable, including related tests and README updates.
- Never commit local database files, virtual environments, caches, or secrets.

## Project

- Python 3.11+, FastAPI, SQLAlchemy 2.1, Alembic, PostgreSQL, and Psycopg 3.
- Application entry point: `app.main:app`.
- Database sessions: `app/database.py`; typed ORM models: `app/models.py`;
  Alembic revisions: `app/migrations/`.
- `DATABASE_URL` is required. Do not add a SQLite fallback or automatic `.env` loading.
- Docker Compose provides local PostgreSQL; its named volume is outside Git.
- Dockerfile builds the API image; Docker Compose runs the API and database.
- The schema includes users, teams, memberships, notes, note shares, and sessions.
- Registration, login, current-user lookup, and logout are implemented under
  `/auth`. Note creation, listing, reading, updates, and deletion are implemented
  under `/notes`.
- Personal notes are owner-only unless shared read-only. Source-team rights
  require current membership, including for the original creator; removing that
  membership revokes those rights. An independent recipient grant may retain read access.
- Team creation, listing, and membership routes are implemented under `/teams`.
  Only owners can add or remove members; all members can view their roster.
  Outsiders receive `404`. Create teams and owner memberships atomically; reject
  owner removal and client-supplied roles.
- Note saves and deletes require the current positive `version`; stale writes
  return `409`. Keep authorization checks and membership locks inside the write
  transaction. Owners alone can mutate private notes; current team members can
  mutate team notes. Preserve ownership and team assignment during saves.
- Note lists support optional trimmed literal search with `q` (1–200 characters).
  Apply current read permissions and search before stable, bounded pagination.
- Read-only grants extend read access only; keep `writable_to` independent.
- Passwords use Argon2id; opaque bearer tokens are stored only as SHA-256 hashes.
  Sessions expire after one hour and logout revokes the current token.

## Commands

Run from the repository root:

```sh
docker compose up --build -d --wait
curl -i http://127.0.0.1:8000/health
docker compose --profile test run --build --rm tests
```

For host development, activate the virtual environment and stop the container API
if it is using port 8000:

```sh
python -m pip install -r requirements-dev.txt
docker compose up -d --wait db
export DATABASE_URL='postgresql://notes:notes@127.0.0.1:5432/notes'
python -m uvicorn app.main:app --reload
```

Docker Compose builds the test image and supplies an isolated test database and
`TEST_DATABASE_URL`; reviewers need no host Python or manual database setup.
Development dependencies belong in `requirements-dev.txt`, outside the runtime
image. The test container runs dependency validation, Ruff, Pyright, and tests.
For host checks, run `python -m pip check`, `python -m ruff check app tests scripts`, and
`python -m pyright`. Keep the shared Python 3.11 target and deprecated-API errors
in `pyproject.toml`; fix warnings instead of suppressing them.

## Database changes

- Keep initialization repeatable and preserve existing data.
- Use typed SQLAlchemy ORM models and SQLAlchemy expressions with bound values;
  do not add handwritten SQL to application queries.
- Use short-lived sessions from `get_session()`. Commit successful transactions,
  roll back failures, and return pooled connections when the session closes.
- Keep database files, virtual environments, caches, and secrets out of Git.
- Use `TEST_DATABASE_URL` and unique test schemas; never change public-schema or
  developer data in tests. The Compose test profile supplies a dedicated test
  database with no published host port.
- Add explicit Alembic Python revisions when changing the schema. Preserve the
  transactional migration runner, advisory lock, revision checks, and adoption
  of validated legacy `schema_migrations` history.
- Do not modify applied migration files; add a new migration for later changes.
- Keep schema migrations in Python Alembic revisions. Compatibility tests should
  reconstruct earlier schemas using those revisions, without separate `.sql` files.
- Preserve any earlier local SQLite database files; importing their data is a
  separate task.

## REST and OpenAPI

- Preserve the documented resource paths and existing JSON payload shapes.
- Give every operation an explicit stable `operation_id`, summary, description,
  and tag. Describe request/response fields and constraints with Pydantic metadata.
- Use `app/api_contract.py` for shared error/validation schemas and response
  headers. Document actual success and failure statuses on each route, including
  `401`, applicable permission/not-found/conflict errors, sanitized `422`, and `500`.
- Protected operations use `BearerAuth` and document `WWW-Authenticate: Bearer` on
  `401`; private response status definitions include `Cache-Control: no-store`.
- Keep `204` responses bodyless. Add `Location` to `201` only when a canonical
  retrievable resource exists, and document the header accurately.
- Bound collection reads with `limit` 1–100 (default 50) and nonnegative BIGINT
  `offset` (default 0). Filter permissions before pagination and document ordering.
  Empty authorized pages return `[]`; an inaccessible parent still returns `404`.
- Preserve safe JSON errors and validation issues containing only `type`, `loc`,
  and `msg`. Never expose submitted passwords, database internals, or tracebacks.
- Run the OpenAPI/HTTP contract tests with the integration suite. Keep generated
  documentation synchronized when adding routes, parameters, schemas, or errors.

## Verification

Run checks relevant to the change. Database changes should verify persistence and
failure rollback. Startup changes should verify the application starts and the
health endpoint still responds. Report what was verified and any remaining limits.
Authentication changes should cover registration, generic login failures, token
expiry and revocation, public-user responses, and `Cache-Control: no-store`.
Note changes should cover private-note isolation, current team membership,
creator access after removal, authenticated ownership, validated input,
pagination after access filtering, inaccessible-note `404`, and no-store headers.
Versioned writes should cover stale-save rejection without data changes, stale
deletes, concurrent saves, membership-removal races, and upgrading existing notes
to version 1 without changing their content, ownership, or timestamps.

Team changes should cover cross-team isolation, owner-only mutations, immediate
revocation after removal, owner protection, and atomic team creation.

## Project requirements

- Include all application source code and tests, plus reproducible setup and test
  commands in README.md.
- Document two or three design decisions that required the most thought, why they
  were chosen, and their tradeoffs.
- Explain what would be changed, added, or stopped with more time.
- Keep these sections aligned with implemented behavior as development proceeds;
  clearly distinguish planned work from completed features.

## Read-only note sharing

- Only personal-note owners or current source-team owners manage grants.
- Grant destinations must be a current team of the caller; revocation must still
  work after the manager leaves the destination.
- Use `visible_to` for reading, and keep `writable_to` independent of grants.
  Recipient membership never permits editing, deletion, or re-sharing.
- Evaluate access before versions, search, or pagination. Return `404` for
  inaccessible resources and do not duplicate notes shared through several teams.
- Sharing changes do not increment the content version or update timestamp.

## End-to-end verification

- `scripts/demo.py` uses only the standard library and runs inside the API image.
  Verify the full HTTP flow with `docker compose exec -T api python scripts/demo.py`.
- Keep passwords and bearer tokens out of demo output. Use unique accounts on each
  run; document which demo records remain in the selected database.
- Preserve Docker-only setup. Include demo scripts in the image and static checks.
- Verify a fresh Compose project and volume; use alternate `API_PORT` and
  `POSTGRES_PORT` values when the development stack is running. Remove only the
  disposable verification project's volume afterward.
