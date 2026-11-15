# Notes API

A FastAPI REST service for a note-taking product shared across teams.

The service supports accounts, teams, private and team notes, versioned editing
and deletion, revocable read-only sharing, and permission-aware search. It runs
with Docker and PostgreSQL, uses typed SQLAlchemy models and Alembic migrations,
and publishes a generated OpenAPI reference.

## Architecture

The current Docker Compose deployment runs one API service and one PostgreSQL
service. OpenAPI is generated from the route declarations and Pydantic models.

```mermaid
flowchart TD
    Client["Client or Swagger UI"] -->|"HTTP / JSON"| API["FastAPI / Uvicorn<br/>Routing, validation, safe errors"]
    API -->|"Protected operations"| Auth["Bearer authentication<br/>Database-backed session lookup"]
    API -->|"Register or sign in"| Routes["Route modules<br/>Auth, teams, notes, sharing"]
    Auth -->|"Authenticated user"| Routes
    API -->|"Public health check"| Health["/health<br/>Process liveness only"]
    API -->|"Public reference"| Docs["Generated OpenAPI<br/>/openapi.json, /docs, /redoc"]
    Routes -.->|"Routes and Pydantic models"| Docs

    Auth -->|"Authentication transaction"| Tx["SQLAlchemy ORM<br/>get_session / Session.begin<br/>Commit or rollback, then close"]
    Routes -->|"Business transaction"| Tx
    Tx --> Pool["Engine connection pool<br/>Per process and URL / Psycopg"]

    Lifecycle["Application lifespan"] -->|"Startup"| Migrate["Alembic revisions<br/>engine.begin and advisory lock"]
    Migrate --> Pool
    Lifecycle -.->|"Shutdown: dispose pool"| Pool
    Pool --> DB[("PostgreSQL<br/>users, teams, memberships<br/>notes, note_shares, sessions<br/>alembic_version")]
    DB --- Volume["Persistent Docker volume"]
```

Each call to `get_session()` creates its own session and transaction. Successful
work commits; exceptions roll back; closing the session returns its connection to
the pool. Authentication lookup and route work use separate transactions. Login
releases its read transaction before password verification and opens another
transaction to store the new session.

Note updates and deletion combine row locks with version checks; stale versions
return `409`. Membership locks protect relevant authorization checks through the
write transaction. Startup applies migrations in a separate engine transaction
under a PostgreSQL advisory lock. The health endpoint checks process liveness;
startup must complete migrations before the application begins serving requests.

## Run locally

The only prerequisite is Docker with Docker Compose. From the project directory,
build and start the API and database:

```sh
docker compose up --build -d --wait
```

The API is available at `http://127.0.0.1:8000`. The Dockerfile uses Python 3.12
and runs the API as a nonroot user. Compose waits for PostgreSQL and the API's
HTTP health check to pass. Stop the services with `docker compose down`; the named
database volume is preserved.


The local host ports default to 8000 for the API and 5432 for PostgreSQL. Set
`API_PORT` and `POSTGRES_PORT` before starting Compose if either is in use. A
separate disposable demo stack can run alongside the development stack; for
example, in a POSIX shell:

```sh
API_PORT=18080 POSTGRES_PORT=15432 docker compose -p notes-demo up --build -d --wait
docker compose -p notes-demo exec -T api python scripts/demo.py
docker compose -p notes-demo down --volumes
```

The final command removes only that demo project's containers and data volume.
The regular development stack and its volume use a different project name.

### Run the HTTP demo

After starting the application, run the complete demo inside the API container:

```sh
docker compose exec -T api python scripts/demo.py
```

The script uses only Python's standard library and needs no host Python, HTTP
client, API token, or manually entered IDs. It registers unique Alice, Bob, and
Carol accounts on each run, creates teams and notes, checks a conflicting save,
read-only sharing, permission-aware search and pagination, revocation, deletion,
and logout. Unexpected statuses or missing no-store headers make it fail with a
nonzero exit code. Passwords and tokens are never printed.

This command writes demo data to the selected database. Three accounts, two
teams, and one private note remain after a successful run; existing data is not
removed. Use a disposable stack for a clean demonstration. When running against
an API started outside Docker, use Python 3.11 or newer:

```sh
python scripts/demo.py --base-url http://127.0.0.1:8000
```

## Try the API

```sh
curl -i http://127.0.0.1:8000/health
```

Expected: HTTP `200 OK` with `{"status":"ok"}`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Confirm the service is running |
| POST | `/auth/register` | Create an account |
| POST | `/auth/login` | Create a one-hour bearer session |
| GET | `/auth/me` | Get the authenticated user |
| POST | `/auth/logout` | Revoke the current session |
| POST | `/teams` | Create a team and become its owner |
| GET | `/teams` | List teams the caller belongs to |
| GET | `/teams/{team_id}/members` | View a team roster as a member |
| POST | `/teams/{team_id}/members` | Add a registered user as a member (owner only) |
| DELETE | `/teams/{team_id}/members/{user_id}` | Remove a member (owner only) |
| POST | `/notes` | Create a private or team note |
| GET | `/notes` | List notes visible to the authenticated user |
| GET | `/notes/{note_id}` | Read a visible note |
| PUT | `/notes/{note_id}` | Save a note using its current version |
| DELETE | `/notes/{note_id}?version={version}` | Delete a note using its current version |
| GET | `/notes/{note_id}/shares` | List grants as the note's sharing manager |
| POST | `/notes/{note_id}/shares` | Grant read-only access to a recipient team |
| GET | `/notes/{note_id}/shares/{team_id}` | Read a managed grant |
| DELETE | `/notes/{note_id}/shares/{team_id}` | Revoke a managed grant |

The health endpoint reports process liveness; it does not query the database.

- [Interactive API documentation](http://127.0.0.1:8000/docs)
- [OpenAPI schema](http://127.0.0.1:8000/openapi.json)

### Register and sign in

```sh
curl -i http://127.0.0.1:8000/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"reader@example.com","display_name":"Sample User","password":"review-only-passphrase"}'

curl -i http://127.0.0.1:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"reader@example.com","password":"review-only-passphrase"}'
```

Registration returns `201` and a public user with `id`, `email`, `display_name`,
and `created_at`. Login returns `200` with `access_token`, `token_type` (`bearer`),
and `expires_at`. Copy the returned token into `TOKEN`:

```sh
TOKEN='paste-access_token-here'
curl -i http://127.0.0.1:8000/auth/me \
  -H "Authorization: Bearer $TOKEN"
```

`/auth/me` returns the same public user fields.

Passwords must contain 12–128 characters. Display names are stripped and must
contain 1–100 characters. Emails are validated and normalized to lowercase.
Unknown input fields and invalid values return `422`; an existing email returns
`409`. Missing, invalid, expired, or revoked credentials return `401`. Login uses
the same error for an unknown account or a wrong password.

Passwords are hashed with Argon2id through Pwdlib. Session tokens are random,
expire after one hour, and are stored only as SHA-256 hashes. These are revocable
database sessions, with no JWT or signing-secret configuration. Authentication
responses use `Cache-Control: no-store`.

### Teams and membership

All team routes require a valid bearer session. A team and its creator's `owner`
membership are saved in one transaction. Team names are stripped and must contain
1–100 characters; duplicate names are allowed. `GET /teams` returns an array of
`id`, `name`, `created_at`, and the caller's `role`, ordered by team ID. It only
returns teams the caller belongs to.

```sh
curl -i http://127.0.0.1:8000/teams \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Engineering"}'

curl -i http://127.0.0.1:8000/teams \
  -H "Authorization: Bearer $TOKEN"
```

Use the returned team ID and another registered user's ID below. User IDs are
returned during registration and by `/auth/me`; there is no public user directory.
Keep the owner's token in `TOKEN` for membership changes:

```sh
TEAM_ID=1    # Replace with the created team's id.
MEMBER_ID=2  # Replace with the registered user's id.

curl -i http://127.0.0.1:8000/teams/$TEAM_ID/members \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"user_id\":$MEMBER_ID}"

curl -i http://127.0.0.1:8000/teams/$TEAM_ID/members \
  -H "Authorization: Bearer $TOKEN"

curl -i -X DELETE http://127.0.0.1:8000/teams/$TEAM_ID/members/$MEMBER_ID \
  -H "Authorization: Bearer $TOKEN"
```

Adding a member returns `201` with `user_id`, `display_name`, `role`, and
`created_at`. Any team member can view the roster, ordered by user ID; it contains
those same fields and excludes account email addresses and credentials. Removing
a member returns an empty `204` response and immediately removes their team and
roster visibility. Membership in other teams is unaffected.

Only owners can add or remove members (`403` for ordinary members). Missing teams
and teams the caller does not belong to both return `404`; this prevents team
existence or member information from being disclosed to outsiders. Adding an
unknown user or removing a missing member returns `404`. Adding an existing
member returns `409`. Owners cannot be removed or demoted (`409`), preserving
team ownership; ownership transfer and member self-service are not implemented.
The API always adds the `member` role and rejects client-supplied roles. Unknown
fields, invalid names, and invalid IDs return `422`. IDs must be positive
PostgreSQL BIGINT values; JSON `user_id` must be an integer, not a boolean or a
string. Team responses, including errors, use `Cache-Control: no-store`.

### Create and read notes

Use the token from login to create a private note and list visible notes:

```sh
curl -i http://127.0.0.1:8000/notes \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"title":"Meeting notes","content":"Agree on next steps."}'

curl -i 'http://127.0.0.1:8000/notes?limit=50&offset=0' \
  -H "Authorization: Bearer $TOKEN"
```

Creation returns `201`, a `Location: /notes/{id}` header, and the note's `id`,
`owner_id`, `team_id`, `title`, `content`, `created_at`, `updated_at`, and `version`. Copy the
returned `id` into `NOTE_ID` to read that note:

```sh
NOTE_ID=1 # Replace with the id returned by creation.
curl -i "http://127.0.0.1:8000/notes/$NOTE_ID" \
  -H "Authorization: Bearer $TOKEN"
```

Titles are stripped and must contain 1–200 characters. Content defaults to an
empty string, preserves whitespace, and accepts up to 100,000 characters. The
owner is always the authenticated user; clients cannot choose `owner_id`.
Omitting `team_id`, or setting it to `null`, creates a private note. A supplied
team ID must be a positive integer, without string or boolean coercion. Invalid
input and unknown fields return `422`.

Personal notes are initially owner-only; explicit shares can add read-only team access.
Supplying a team ID requires
current membership in that team; both `owner` and `member` roles can create and
read team notes. Unknown teams and teams the caller does not belong to return
`404`. Removing source-team membership revokes access through that team,
including for the original creator. An independent recipient-team grant can
retain read-only access. Private notes are not automatically shared with teams.

Listing returns a plain JSON array containing the caller's private notes and
notes for teams they currently belong to. `limit` defaults to 50 and accepts
1–100; `offset` defaults to 0 and must be nonnegative. Reading a missing or
inaccessible note returns the same `404` response. Note responses use
`Cache-Control: no-store`.

Read routes also include notes shared read-only with a recipient team the caller currently belongs to.

### Search and paginate notes

Use the optional `q` parameter to search note titles and content:

```sh
curl -i --get http://127.0.0.1:8000/notes \
  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode 'q=meeting' \
  --data-urlencode 'limit=20' \
  --data-urlencode 'offset=0'
```

Search matches a case-insensitive literal substring. Leading and trailing query
whitespace is removed; the remaining query must contain 1–200 characters. Empty,
whitespace-only, or oversized queries return `422`. SQL wildcard characters such
as `%` and `_` are literal text. Omitting `q` lists all visible notes.

Current ownership, source-team membership, and recipient-team grants are checked
before search and pagination. Revoking a grant or removing membership removes
search access too. Multiple matching grants return each note once. Results use
the existing JSON array format, ordered by creation time descending and then ID
descending. `limit` defaults to 50 and accepts 1–100; `offset` defaults to zero
and accepts nonnegative PostgreSQL BIGINT values. No matches or an exhausted page
returns `200` with `[]`. Search does not change notes or their versions.

### Share notes read-only with another team

A personal-note owner or a current owner of a note's source team can manage its
shares. Ordinary source-team members, including the original author, cannot
manage team-note shares. To grant access, the caller must also currently belong
to the recipient team. Create a recipient team or use one from `GET /teams`, then
substitute its returned ID below:

```sh
RECIPIENT_TEAM_ID=2 # Replace with an existing team you currently belong to.
curl -i "http://127.0.0.1:8000/notes/$NOTE_ID/shares" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"team_id\":$RECIPIENT_TEAM_ID}"

curl -i "http://127.0.0.1:8000/notes/$NOTE_ID/shares" \
  -H "Authorization: Bearer $TOKEN"

curl -i -X DELETE "http://127.0.0.1:8000/notes/$NOTE_ID/shares/$RECIPIENT_TEAM_ID" \
  -H "Authorization: Bearer $TOKEN"
```

Creation returns `201` with `note_id`, `team_id`, and `created_at`. Its `Location`
identifies an individual grant that its manager can retrieve with `GET`.
Recipient-team members can read the note and see it in their note listing; the
grant never permits editing, deletion, or re-sharing. Losing recipient membership
or revoking the grant removes that read access on subsequent requests. Independent
source membership or another grant can still provide access. Managers may revoke
a grant after leaving its recipient team. Sharing changes neither the content
version nor its update timestamp.

Share listing is manager-only, with the same `limit` and `offset` bounds as note
listing. It orders by creation time descending, then recipient team ID descending.
Missing or unmanaged notes return `404`; duplicate grants and attempts to share
back to the source team return `409`. Revocation returns an empty `204`. All share
responses, including errors, use `Cache-Control: no-store`.

### Update and delete notes

Every new and existing note starts at `version: 1`. Use the version from the
latest read when saving. `PUT /notes/{id}` requires exactly `title`, `content`,
and `version`; it replaces the title and content and returns the full saved note.
Titles and content follow the same validation rules as creation, and `version`
must be a positive integer. Both fields are required, including `content: ""`
when clearing a note. Note ownership, team, and creation time are preserved.

```sh
NOTE_VERSION=1 # Use the version returned by the latest read.
curl -i -X PUT "http://127.0.0.1:8000/notes/$NOTE_ID" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"title\":\"Meeting notes\",\"content\":\"Agreed next steps.\",\"version\":$NOTE_VERSION}"
```

A successful save returns `200`, increments `version` by one, and updates
`updated_at`. If two users save version 1, one succeeds with version 2 and the
other receives `409`; the rejected save leaves the stored note unchanged. Read
the latest note and reconcile changes before retrying. Versions remain within
the positive PostgreSQL BIGINT range; an exhausted version cannot be incremented.

Set `NOTE_VERSION` to the version returned by the latest read or save before
deleting:

```sh
NOTE_VERSION=2 # Replace with the note's current version.
curl -i -X DELETE "http://127.0.0.1:8000/notes/$NOTE_ID?version=$NOTE_VERSION" \
  -H "Authorization: Bearer $TOKEN"
```

Deletion requires a matching version and returns an empty `204`. A stale version
returns `409` and preserves the note. A missing version or invalid input returns
`422`. A deleted or inaccessible note returns `404` on subsequent requests.

Private notes can be edited or deleted only by their owner. Any current member
of a note's team can edit or delete it, including notes created by another member.
Removing membership also revokes write access for the note's original creator.
Authorization is checked before reporting a version conflict, so outsiders get
the same `404` as for missing notes. Writes hold the note and membership locks
until the transaction commits, preserving these checks during concurrent saves
and membership removal. All note responses, including errors, use
`Cache-Control: no-store`.

### Log out when finished

```sh
curl -i -X POST http://127.0.0.1:8000/auth/logout \
  -H "Authorization: Bearer $TOKEN"
```

Logout returns an empty `204` response and immediately revokes that token;
using it afterward returns `401`.

## Database

The service uses SQLAlchemy 2.1's typed ORM with PostgreSQL and the Psycopg 3
driver. Models use `DeclarativeBase`, `Mapped`, and `mapped_column`; queries use
SQLAlchemy expressions and bound parameters. `DATABASE_URL` is required; there
is no SQLite fallback or automatic `.env` loading. Set this environment variable
to use another PostgreSQL server. Existing `postgresql://` URLs remain supported
and select the Psycopg 3 driver.

An engine manages pooled connections. Each `get_session()` context opens a
short-lived session, commits successful work, and rolls back failures before
closing. Response models contain the selected public fields, including when ORM
objects hold password hashes or session data.

Docker Compose runs PostgreSQL 18 with database, username, and password `notes`.
The development port is bound to `127.0.0.1:5432`. A named volume mounted at
`/var/lib/postgresql` keeps database data outside Git. These credentials are for
local development.

Compose supplies the API container with
`postgresql://notes:notes@db:5432/notes`; host processes use `127.0.0.1` instead of
the Compose hostname `db`.

Application startup applies Alembic Python revisions in a transaction. A
PostgreSQL advisory lock serializes migration runs, and `alembic_version` records
the current revision. Initialization is repeatable and rejects unknown revisions.
Databases with a compatible `schema_migrations` ledger are adopted by validating
their contiguous history, stamping the matching Alembic revision, and applying
later revisions. Existing users, notes, and sessions are preserved. Schema
changes use versioned Python Alembic revisions; separate `.sql` files are not
required. Compatibility tests construct earlier schemas from those revisions.

To apply migrations without starting the server:

```sh
docker compose run --rm api python -c 'from app.database import init_database; init_database()'
```

The schema contains:

| Table | Purpose |
| --- | --- |
| `users` | User records with case-insensitive unique email addresses |
| `teams` | Team records |
| `memberships` | User membership in teams, with `owner` or `member` roles |
| `notes` | Notes with an owner, optional source team, and optimistic version |
| `note_shares` | Revocable read-only grants to current recipient-team members |
| `sessions` | Hashed bearer tokens, user ownership, and expiration |
| `alembic_version` | Current Alembic revision |
| `schema_migrations` | Preserved migration history on upgraded legacy databases |

Foreign keys preserve relationships, and timestamps use PostgreSQL
`timestamptz`. Revision `002_sessions` adds authentication storage,
`003_note_versions` initializes existing notes at version 1, and `004_note_shares`
adds recipient grants. Read routes check current ownership and source/recipient
memberships; write routes require the private owner or current source membership.
Team routes enforce current membership and owner permissions.

Any earlier `data/notes.db` file is left untouched. Its contents are not imported,
and `NOTES_DB_PATH` is no longer used.

## Verify

Run dependency validation, linting, type checking, and the full test suite using
Docker. No host Python installation, virtual environment, database creation, or
environment-variable setup is required:

```sh
docker compose --profile test run --build --rm tests
```

The test image includes the application and test source. Compose starts an
isolated `test-db` service, supplies `TEST_DATABASE_URL`, and waits for the database
to be ready. The test database has no published host port and uses temporary
storage. Each integration test creates and removes a unique schema; development
data is untouched. The test target installs `requirements-dev.txt`, including
HTTPX2, Ruff, Pyright, and OpenAPI/JSON Schema validators. It runs `pip check`,
Ruff, and Pyright before the integration tests with deprecation warnings treated as errors. The API image
excludes the test source and these development dependencies. Migration tests also
cover adopting the previous schema and preserving data when an upgrade fails.

To stop both development and test services afterward, use
`docker compose --profile test down`; the named development database volume is
preserved.

## Optional host development

To run the API directly with automatic reload, use Python 3.11 or newer. If the
container API is already running, stop it with `docker compose stop api` to free
port 8000. Then run:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
docker compose up -d --wait db
export DATABASE_URL='postgresql://notes:notes@127.0.0.1:5432/notes'
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

On Windows, activate the environment with `.venv\Scripts\activate` instead and
set environment variables using your shell's syntax. Stop the server with Ctrl+C.

### Editor and Python checks

In VS Code, install the recommended Python, Pylance, and Ruff extensions and use
**Python: Select Interpreter** to select this project's `.venv`. Pylance reads
the shared `pyproject.toml` configuration, checks the workspace, and treats
deprecated API use as an error. Keep editor extensions updated through VS Code;
the pinned command-line checkers provide reproducible CI results.

Run the same static checks locally from the project directory:

```sh
python -m pip check
python -m ruff check app tests scripts
python -m pyright
```

Type checking targets the minimum supported Python 3.11, including the models,
Alembic revisions, and tests. Context managers use `Generator` or
`AsyncGenerator` from `collections.abc`, with explicit parameters compatible
with Python 3.11 and 3.12. The lifespan function is annotated
`AsyncGenerator[None, None]`; deprecated iterator annotations are not suppressed.

## REST and OpenAPI contract

The application generates OpenAPI 3.1 from the same route declarations and
Pydantic models used at runtime. Use [Swagger UI](http://127.0.0.1:8000/docs) to
try requests, [ReDoc](http://127.0.0.1:8000/redoc) to read the reference, or
[the JSON schema](http://127.0.0.1:8000/openapi.json) to generate a client. In Swagger
UI, sign in through `/auth/login`, then paste the returned token into **Authorize**.
Each operation has an explicit stable ID, descriptions, success/error models,
parameter constraints, and applicable response headers.

- Resource collections use plural nouns and nested membership/share resources.
  `GET` reads, `POST` creates or performs the named authentication action, `PUT`
  replaces a note's editable title/content, and `DELETE` removes a resource.
- Reads return `200`, creations return `201`, and successful deletions/logout
  return an empty `204`. Note and share creation return a `Location` pointing to
  their retrievable canonical resource. Registration and team/member creation
  do not advertise a canonical resource URL that the API does not provide.
- All collection reads accept `limit` (default 50, range 1–100) and `offset`
  (default 0, range 0–9223372036854775807). Access filtering happens before
  pagination. Notes and shares sort by creation time descending and a descending
  ID tie-breaker; teams and rosters sort by team/user ID ascending. Empty or
  exhausted pages return `[]`. An inaccessible parent resource still returns `404`.
- Note lists optionally accept `q`: a trimmed 1–200 character, case-insensitive
  literal substring search across title and content. Search and permission
  filtering happen before pagination; `%` and `_` are literal characters.
- Protected operations require an opaque bearer token. Missing, invalid, expired,
  or revoked tokens return `401` with `WWW-Authenticate: Bearer`. Insufficient
  team roles return `403`; inaccessible notes, shares, and teams use `404` where
  exposing existence would leak information. Private responses, including errors,
  use `Cache-Control: no-store`.
- Application errors use `{"detail":"message"}`. Request validation returns
  `422` with `{"detail":[{"type":"missing","loc":["body","title"],"msg":"Field required"}]}`.
  Validation issues omit submitted values and validator context; password fields
  are documented as write-only. Unexpected failures return a generic JSON `500`
  without database details, credentials, or tracebacks.
- Note updates require both editable fields and the latest `version` in the
  request body; deletion requires the version query parameter. A stale authorized
  write returns `409` without changing data. Read the current note before retrying.
  Read-only sharing never grants update, deletion, or share-management permission.
- Unsupported methods return `405` with an `Allow` header listing every supported
  method for that resource.
- Resource IDs are positive PostgreSQL BIGINT values. Timestamps are RFC 3339
  date-time strings with a UTC offset. Unknown JSON body fields are rejected;
  request limits and examples are included in the generated schema.

`tests/test_api_contract.py` validates the generated OpenAPI document, checks
operation/security/status/header conventions, and validates actual HTTP response
bodies against their published JSON schemas. A PostgreSQL-backed flow checks
registration, login, creation, canonical retrieval, and logout. These checks run
with the regular Docker verification command and treat deprecations as errors.
OpenAPI and JSON Schema validators are development dependencies only.

## Project layout

```text
app/
  __init__.py
  api_contract.py   # Shared OpenAPI response models, headers, and conventions
  auth.py           # Authentication routes and request/response models
  database.py       # Pooled ORM sessions and Alembic migration runner
  main.py           # Application and health endpoint
  migrations/       # Versioned Alembic Python revisions
  models.py         # Typed SQLAlchemy database models
  notes.py          # Note CRUD, version checks, visibility, and search
  sharing.py        # Revocable read-only recipient-team grants
  security.py       # Password hashing and token helpers
  teams.py          # Team and owner-managed membership routes
tests/
  test_api_contract.py # OpenAPI validation and HTTP/schema consistency
  test_auth.py      # Authentication HTTP integration tests
  test_teams.py     # Team authorization and membership integration tests
  test_database.py  # Connection and persistence behavior tests
  test_migrations.py # Migration behavior tests
  test_notes.py     # Note creation/read access tests
  test_note_mutations.py # Versions, writes, deletion, and transaction races
  test_search.py    # Literal search, access filtering, and pagination
  test_sharing.py   # Read-only grants and revocation
  test_acceptance.py # Full API collaboration and persistence scenarios
scripts/
  demo.py           # Complete HTTP demonstration using the standard library
AGENTS.md           # Development instructions
Dockerfile          # Runtime and test container stages
compose.yaml        # API, PostgreSQL, and isolated test services
requirements.txt    # Runtime dependencies
requirements-dev.txt # Test dependencies
pyproject.toml      # Shared Pyright and Ruff configuration
```

All application source, migrations, and tests are included in this repository.
A fresh checkout can be initialized using the commands above without copying
database files.

## Commit conventions

Use `type(scope): short description`, with an optional scope and an imperative
subject, for example `feat(db): add note ownership`. Keep commits focused and
runnable, including relevant tests and documentation. Common types are `feat`,
`fix`, `docs`, `test`, `refactor`, and `chore`. See AGENTS.md for project guidance.

## Continuous integration

GitHub Actions runs the Docker-only validation command, including dependency
compatibility, Ruff lint rules, Pyright types and deprecations, and PostgreSQL
integration tests. It then builds and starts the application stack, checks the
HTTP endpoints, verifies that the API container runs as a nonroot user, and runs
the complete HTTP collaboration demo. The workflow uses a pinned checkout action and read-only repository permissions. No
database credentials or service setup are required in repository settings.

## Design choices and tradeoffs

These decisions describe implemented behavior; planned extensions are below.

1. **Typed SQLAlchemy models, pooled sessions, and Alembic migrations.** ORM
   expressions keep queries consistent with the Python models and let the driver
   bind values. Sessions commit successful work, roll back failures, and return
   connections to the pool. Reviewed Alembic revisions and an advisory lock
   preserve atomic schema upgrades, including adoption of earlier migration
   history. The tradeoffs are an ORM and migration dependency, pool lifecycle
   management, and maintaining explicit revisions alongside model changes.
2. **Argon2id passwords and revocable opaque sessions.** Argon2id provides password
   hashing, while random bearer tokens are stored only as SHA-256 hashes. Sessions
   expire after one hour and logout revokes them immediately. This avoids JWT
   signing-key management and delayed revocation, at the cost of a database lookup
   for each authenticated request and password-hashing work during login.
3. **Membership-filtered queries and versioned writes.** Private-note access
   checks ownership; team-note access checks current membership, including for the
   original creator. This makes membership removal take effect on subsequent reads without
   copying access lists onto each note. Filtering also happens before pagination,
   and missing and inaccessible notes share a `404` response. The tradeoff is more
   involved queries and a membership lookup for team access; these are covered by
   API tests against isolated PostgreSQL schemas. Saves and deletes lock the note,
   require the caller's current version, and check current membership in the same
   transaction. Conflicting saves return `409` instead of overwriting another
   writer's changes. Clients must reload and reconcile conflicts; this provides
   safe shared saves without simultaneous character-level editing. Read-only grants
   extend the read predicate without changing write permissions. Literal substring
   search shares that read predicate; simple bounded offset pages keep the API
   straightforward, while full-text indexes and cursor pagination remain future
   optimizations for measured workloads.

## What I would change, add, or stop with more time

With more time, I would prioritize:

- **Change:** Tune connection-pool sizing and add full-text/trigram search indexes
  when measured traffic and query plans justify them. Use cursor pagination when
  stable traversal during concurrent inserts becomes more important than simple
  offset pages.
- **Add:** Login throttling, email verification, and password reset flows before
  opening account registration beyond the development environment.
- **Add:** Note revision history and recovery, followed by an audit trail for
  sharing and membership changes. These would make edits recoverable and help
  teams understand changes to access.
- **Add:** Backup and restore checks and operational visibility such as structured
  logs and database readiness checks before a production rollout.
- **Stop:** Applying migrations during every application startup once deployment
  coordination becomes more involved. Move them into a controlled release step
  while retaining the same version ledger and failure checks.
