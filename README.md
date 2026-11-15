# Notes API

A FastAPI REST service for a note taking product shared across teams.

The service supports accounts, teams, private and team notes, versioned editing
and deletion, revocable read only sharing, and permission aware search. It runs
with Docker and PostgreSQL, uses typed SQLAlchemy models and Alembic migrations,
and publishes a generated OpenAPI reference.

## Quick start

With Docker and Docker Compose running, execute this single command from the
cloned repository:

```sh
docker compose run --build --rm demo
```

It builds and starts PostgreSQL and the API, applies migrations, then runs the
complete demonstration with automatically created accounts and bearer sessions.
Successful checks print `OK` and finish with `Demo passed`; any failed check exits
with an error. No `.env`, existing account, token, or cloud configuration is needed.
After startup, open the [local Swagger UI](http://127.0.0.1:8000/docs) on the
computer running Docker. All `127.0.0.1` URLs in this guide refer to that computer
and become available when the local API is running. GitHub hosts the project
source; these links open your locally running service.
This creates demo records in the local database; see the demo details below.

## Architecture

The default Docker Compose deployment runs one API service and one PostgreSQL
service. The scale profile runs two API replicas behind NGINX; the cloud manifests
use an external PostgreSQL service. OpenAPI is generated from route declarations
and Pydantic models.

```mermaid
flowchart TD
    Client["Client or Swagger UI"] -->|"HTTP / JSON"| API["FastAPI / Uvicorn<br/>One worker per replica"]
    Client -.-> Gateway["Scale profile: NGINX<br/>Cloud: ingress with TLS"]
    Gateway --> API
    API -->|"Protected operations"| Auth["Bearer authentication<br/>Database backed session lookup"]
    API -->|"Register or sign in"| Routes["Route modules<br/>Auth, teams, notes, sharing"]
    Auth -->|"Authenticated user"| Routes
    API -->|"Public health check"| Health["/health: process liveness<br/>/ready: database and schema"]
    API -.-> Metrics["Per replica Prometheus metrics<br/>Private cloud scrape with bearer token"]
    Health -->|"Only /ready"| Pool
    API -->|"Public reference"| Docs["Generated OpenAPI<br/>/openapi.json, /docs, /redoc"]
    Routes -.->|"Routes and Pydantic models"| Docs

    Auth -->|"Authentication transaction"| Tx["SQLAlchemy ORM<br/>get_session / Session.begin<br/>Commit or rollback, then close"]
    Routes -->|"Business transaction"| Tx
    Maintenance["Session cleanup CronJob"] --> Tx
    Tx --> Pool["Engine connection pool<br/>Per process and URL / Psycopg"]

    Lifecycle["Local startup or cloud release Job"] --> Migrate["Alembic revisions<br/>engine.begin and advisory lock"]
    Migrate --> Pool
    Lifecycle -.->|"Shutdown: dispose pool"| Pool
    Pool --> DB[("PostgreSQL<br/>users, teams, memberships<br/>notes, note_shares, sessions<br/>login_throttles, alembic_version")]
    DB --- Volume["Laptop: persistent Docker volume<br/>Cloud: provider storage and backups"]
```

Each call to `get_session()` creates its own session and transaction. Successful
work commits; exceptions roll back; closing the session returns its connection to
the pool. Authentication lookup and route work use separate transactions. Login
releases its read transaction before password verification and opens another
transaction to store the new session.

Note updates and deletion combine row locks with version checks; stale versions
return `409`. Membership locks protect relevant authorization checks through the
write transaction. Local startup applies migrations in a separate engine
transaction under a PostgreSQL advisory lock. Cloud replicas use `AUTO_MIGRATE=false`
and require a successful separate migration Job. `/health` checks process liveness;
`/ready` checks the database connection and expected schema revision.

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
changed `API_PORT` replaces `8000` in the local URLs below; a changed
`POSTGRES_PORT` replaces `5432` in host database connection URLs. A
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
read only sharing, permission aware search and pagination, revocation, deletion,
and logout. Unexpected statuses or missing `no-store` headers make it fail with a
nonzero exit code. Passwords and tokens are never printed.

The demo logs out its generated sessions after the checks. To make requests
manually, [register your own account and sign in](#register-and-sign-in).

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
| GET | `/ready` | Check database connectivity and schema readiness |
| POST | `/auth/register` | Create an account |
| POST | `/auth/login` | Create a one hour bearer session |
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
| POST | `/notes/{note_id}/shares` | Grant read only access to a recipient team |
| GET | `/notes/{note_id}/shares/{team_id}` | Read a managed grant |
| DELETE | `/notes/{note_id}/shares/{team_id}` | Revoke a managed grant |

The health endpoint reports process liveness; it does not query the database.
The readiness endpoint checks the database and accepted migration revision,
returning `503` when the instance is unavailable.

### Local API documentation

Start the application with the quick start command before opening these links
on the same computer:

- [Local interactive API documentation (Swagger UI)](http://127.0.0.1:8000/docs)
- [Local OpenAPI JSON document](http://127.0.0.1:8000/openapi.json)
- [Local API reference (ReDoc)](http://127.0.0.1:8000/redoc)

The Swagger UI and ReDoc pages load their interface assets from a CDN, so the
browser needs internet access to display them.

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

In the local Swagger UI, use **Try it out** on `POST /auth/register`, then
`POST /auth/login`. Copy the response's `access_token`, click **Authorize**, and
paste only the token value. Swagger adds the `Bearer` prefix automatically.

Passwords must contain 12–128 characters. Display names are stripped and must
contain 1–100 characters. Emails are validated and normalized to lowercase.
Unknown input fields and invalid values return `422`; an existing email returns
`409`. Missing, invalid, expired, or revoked credentials return `401`. Login uses
the same error for an unknown account or a wrong password.

Passwords are hashed with Argon2id through Pwdlib. Session tokens are random,
expire after one hour, and are stored only as `SHA-256` hashes. These are revocable
database sessions, with no JWT or signing secret configuration. Authentication
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
team ownership; ownership transfer and member self service are not implemented.
The API always adds the `member` role and rejects client supplied roles. Unknown
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

Personal notes are initially owner only; explicit shares can add read only team access.
Supplying a team ID requires
current membership in that team; both `owner` and `member` roles can create and
read team notes. Unknown teams and teams the caller does not belong to return
`404`. Removing source team membership revokes access through that team,
including for the original creator. An independent recipient team grant can
retain read only access. Private notes are not automatically shared with teams.

Listing returns a plain JSON array containing the caller's private notes and
notes for teams they currently belong to. `limit` defaults to 50 and accepts
1–100; `offset` defaults to 0 and must be nonnegative. Reading a missing or
inaccessible note returns the same `404` response. Note responses use
`Cache-Control: no-store`.

Read routes also include notes shared read only with a recipient team the caller currently belongs to.

### Search and paginate notes

Use the optional `q` parameter to search note titles and content:

```sh
curl -i --get http://127.0.0.1:8000/notes \
  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode 'q=meeting' \
  --data-urlencode 'limit=20' \
  --data-urlencode 'offset=0'
```

Search matches a case insensitive literal substring. Leading and trailing query
whitespace is removed; the remaining query must contain 1–200 characters. Empty,
whitespace only, or oversized queries return `422`. SQL wildcard characters such
as `%` and `_` are literal text. Omitting `q` lists all visible notes.

Current ownership, source team membership, and recipient team grants are checked
before search and pagination. Revoking a grant or removing membership removes
search access too. Multiple matching grants return each note once. Results use
the existing JSON array format, ordered by creation time descending and then ID
descending. `limit` defaults to 50 and accepts 1–100; `offset` defaults to zero
and accepts nonnegative PostgreSQL BIGINT values. No matches or an exhausted page
returns `200` with `[]`. Search does not change notes or their versions.

### Share notes read only with another team

A personal note owner or a current owner of a note's source team can manage its
shares. Ordinary source team members, including the original author, cannot
manage team note shares. To grant access, the caller must also currently belong
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
Recipient team members can read the note and see it in their note listing; the
grant never permits editing, deletion, or resharing. Losing recipient membership
or revoking the grant removes that read access on subsequent requests. Independent
source membership or another grant can still provide access. Managers may revoke
a grant after leaving its recipient team. Sharing changes neither the content
version nor its update timestamp.

Share listing is manager only, with the same `limit` and `offset` bounds as note
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
short lived session, commits successful work, and rolls back failures before
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
the current revision. Revisions `001_initial` through `006_login_throttle` form
one ordered chain; these are incremental changes, not competing initial schemas.
Applied revisions stay immutable, and later schema changes add a new revision.

Alembic is the active migration authority. Databases with an older
`schema_migrations` ledger are adopted only after their contiguous history and
claimed physical schema have been validated. Adoption stamps the corresponding
Alembic revision, including when an empty `alembic_version` table already exists,
then applies later revisions. The old ledger remains archival history.
When both ledgers contain revisions, their histories must agree. Unknown or
abbreviated revisions, multiple current heads, contradictory histories, and
untracked application tables stop initialization before migration DDL runs.
Legacy checks recognize the original v1/v2 PostgreSQL schema signatures, including
their constraints, indexes, defaults, and generated identifiers. An altered or
unrecognized signature requires schema review before adoption; the runner leaves
the existing ledger and application data intact.
Existing users, notes, and sessions are preserved. Schema
changes use versioned Python Alembic revisions; separate `.sql` files are not
required. Compatibility tests construct earlier schemas from those revisions.

To apply migrations without starting the server:

```sh
docker compose run --rm api python -m scripts.migrate
```

The schema contains:

| Table | Purpose |
| --- | --- |
| `users` | User records with case insensitive unique email addresses |
| `teams` | Team records |
| `memberships` | User membership in teams, with `owner` or `member` roles |
| `notes` | Notes with an owner, optional source team, and optimistic version |
| `note_shares` | Revocable read only grants to current recipient team members |
| `sessions` | Hashed bearer tokens, user ownership, and expiration |
| `login_throttles` | Shared hashed account/address login budgets |
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

## Production safeguards and measurement

These safeguards are implemented and can be exercised
on the laptop. Cloud deployment uses the same image and API behavior; the manifests
require an external PostgreSQL service and real deployment secrets. Capacity and
high availability objectives still require measurements in the chosen environment.

The demonstration gateway and example NGINX ingress allow request bodies up to
2 MiB, including maximum length Unicode notes serialized with JSON escapes.

### Database limits and release operations

Each API process has a bounded connection pool. Environment settings are validated
at startup/configuration use; existing URL options such as a test schema search
path are preserved. Defaults are:

| Setting | Default | Purpose |
| --- | ---: | --- |
| `DB_POOL_SIZE` | 5 | Persistent connections per process |
| `DB_MAX_OVERFLOW` | 5 | Additional connections under bursts |
| `DB_POOL_TIMEOUT_SECONDS` | 3 | Maximum pool wait |
| `DB_CONNECT_TIMEOUT_SECONDS` | 5 | Connection establishment timeout |
| `DB_STATEMENT_TIMEOUT_MS` | 10000 | Bound request SQL execution |
| `DB_LOCK_TIMEOUT_MS` | 3000 | Bound lock waits |
| `DB_IDLE_TRANSACTION_TIMEOUT_MS` | 15000 | End abandoned idle transactions |
| `DB_MIGRATION_TIMEOUT_MS` | 120000 | Separate bounded DDL budget |
| `DB_TRIGRAM_SCHEMA` | `notes_extensions` | Namespace containing `pg_trgm` |
| `AUTO_MIGRATE` | `true` | Local convenience; `false` for cloud API replicas |
| `DB_COMPATIBLE_REVISIONS` | empty | Explicit reviewed compatibility allowances during additive rollouts |

Transactions roll back on query/lock failures. Temporary pool/database resource
failures return safe JSON `503` responses with `Retry-After`; the application does
not automatically replay writes with uncertain outcomes. Pool liveness checks
recover stale connections on checkout. `/ready` returns `503` for a database or
migration mismatch, while `/health` remains a process liveness probe.

The scale overlay has two single worker API containers, each with a maximum of
10 connections. Kubernetes adds one rolling update surge pod; terminating pods
may retain their pools while draining. With an allowance for two draining pods,
plan `(2 + 1 + 2) × 1 × (5 + 5) = 50` API connections, plus migration,
maintenance, monitoring, and administrative reserves. This is a planning budget,
not a hard global cap. Recalculate it when changing replicas or rollout/drain
behavior, and keep the total below PostgreSQL's connection limit. The deployment
guide includes explicit migration owner/runtime role grants and a nonowner CRUD
verification flow.
Readiness accepts only the image's own migration head by default. Reviewed additive
rollouts can explicitly allow a declared future revision; follow the staged
compatibility procedure in the deployment guide before migrating old pods.

```sh
docker compose -f compose.yaml -f compose.scale.yaml up --build -d --wait
```

Use an independent Compose project and unused port settings when keeping the
normal stack running. See [deployment instructions](deploy/README.md) for portable
Kubernetes manifests, migration before rollout commands, secrets, resource limits,
readiness/liveness probes, TLS ingress, and per pod metrics scraping. Local
PostgreSQL is one instance; cloud database high availability and managed backups
are configured through the selected database provider.

### Indexed queries and cursor pagination

Alembic revision `005_query_indexes` adds ordered global/owner/source team indexes
and GIN trigram indexes for title/content substring search. PostgreSQL's trusted
`pg_trgm` extension is installed in `notes_extensions` if missing. If a managed
database already installs it elsewhere, set `DB_TRIGRAM_SCHEMA` to that schema
before migrations and application startup; existing extensions are never moved.
Only the migration role needs creation privileges. Queries as short as one or two
characters may still scan many rows; actual query plans determine usefulness.

`GET /notes` retains its array response and offset parameters, and also accepts an
opaque `cursor`. When more visible notes exist, `X-Next-Cursor` and `Link: ...;
rel="next"` identify the next page. Follow the link or reuse the cursor with the
same account and normalized `q`. Cursor and nonzero offset cannot be combined.
Permissions are checked again on every page. Prefer cursor pagination for deep
continuation; offset remains for existing clients.

```sh
# DATABASE_URL must select a disposable/representative test dataset.
python -m scripts.query_plans --user-id 1 --term rarebenchmarkneedle \
  --offset 4000 --analyze --output work/query-plans.json
```

This host command requires Python 3.11+ and the project dependencies. Create and
activate the virtual environment as shown in [Optional host development](#optional-host-development),
then set `DATABASE_URL` for the selected database. The diagnostic connects
directly to PostgreSQL.

The diagnostic uses real `EXPLAIN (ANALYZE, BUFFERS)` results and records which
indexes PostgreSQL actually chose. It does not disable sequential scans or claim
that every query should use an index.

### Operational controls

Login consumes shared PostgreSQL account/address budgets before expensive Argon2
verification. Defaults are 10 attempts per account and 50 per client address per
60 seconds, for both successful and failed attempts. Configure
`LOGIN_RATE_LIMIT_ATTEMPTS`, `LOGIN_IP_RATE_LIMIT_ATTEMPTS`, and
`LOGIN_RATE_LIMIT_WINDOW_SECONDS`. Excess attempts return generic `429` responses
with `Retry-After`. Stored bucket keys are hashed. Trusted proxy configuration is
required to preserve real client addresses; spoofed forwarded headers are not
accepted on a directly exposed API.

Registration and login share a per process password hashing admission limit:
`PASSWORD_HASH_CONCURRENCY=2` by default (allowed range 1–8). The existing Argon2id
parameters remain unchanged. When both slots are occupied, requests receive a
safe `503` with `Retry-After: 1` instead of queuing more memory intensive hashes.
The recommended hash uses about 64 MiB per operation, plus process overhead;
budget `workers × PASSWORD_HASH_CONCURRENCY × 64 MiB` within each container's
memory limit. The cloud example uses one worker, two slots, and a 512 MiB limit.
Increase these settings only after measuring CPU and peak memory together.

`/metrics` exposes per process Prometheus request counts, duration histograms,
connection pool gauges, and process metrics. Labels use route templates, never
raw note IDs, user identities, query text, or tokens. Set `METRICS_TOKEN` for bearer
authentication; cloud manifests require it and the local gateway keeps the metrics
path private. Scrape each replica separately. The laptop default permits a direct
local scrape when the token is unset. Keep one worker per replica for these
per process metrics; using multiple workers in one container requires a
Prometheus multiprocess collection setup before the scrape represents every worker.

Expired sessions and inactive throttle windows are removed in bounded transactions
using `SKIP LOCKED`, so concurrent cleanup jobs do not wait on the same batch:

```sh
docker compose run --rm api python -m scripts.cleanup_sessions --batch-size 1000
```

A Kubernetes CronJob schedules this command. Expired token checks remain enforced
on every request even before cleanup runs.

### Load testing and backup recovery

The load tool creates its own uniquely named, disposable Compose project, chooses
unused local ports, seeds synthetic accounts/teams/private and shared notes, and
runs paced HTTP traffic through two API replicas. It never selects the developer
database. The mix includes listing/search, direct reads, versioned writes, and
login. It reports p50/p95/p99 latency, actual throughput, planned/offered arrivals,
scheduling lag and dropped arrivals, errors, version conflicts, throttling, sampled database connections, and
lock waits. Sampling may miss brief peaks. Choose acceptance budgets before making
a production capacity claim.

The load driver requires Python 3.11+ and Docker Compose on the host. It uses
Python's standard library; application dependencies run inside its containers.

```sh
python3 -m scripts.load_test --duration 15 --rate 50 --concurrency 16 \
  --users 8 --notes 10000 --output work/load-test-report.json
```

Use `--keep-stack` only when inspecting the disposable dataset afterward; the
command prints its unique project name for explicit cleanup. Local observations
describe the local environment; cloud capacity requires separate measurement.

A measured laptop run used 10,000 notes, eight users, two API replicas, a 15 second
window and a target of 50 requests/second: 750/750 requests returned `200`, with
50.04 completed requests/second, p50 4.99 ms, p95 62.43 ms and p99 167.50 ms.
No arrivals were dropped; sampled database connections peaked at eight and no lock
waits were observed. These are one local run, with client scheduling and Docker
sampling overhead recorded, rather than an upper capacity bound.

On a matching 10,000 note dataset, actual PostgreSQL plans selected the ordered note index.
At offset 4,000, deep offset execution measured 1.028 ms versus 0.049 ms for a
keyset query at the same boundary. Selective substring search selected both GIN
trigram indexes and measured 0.283 ms. Query timing excludes client/network time
and varies with cache state and data distribution; it is separate from HTTP latency.

`scripts.backup_restore` uses PostgreSQL custom format `.dump` archives, with
credentials read from environment variables. It refuses to overwrite an existing
backup or restore into a nonempty/same database. Restore is one transaction and
stops on errors. Use PostgreSQL 18 client tools on a cloud operations host, or the
local database container's tools with `--db-container`. The Python wrapper
requires the project dependencies in an activated host virtual environment; see
[Optional host development](#optional-host-development).

Put host, port, database, and credentials in the URL's main fields. Routing
overrides in query parameters are rejected, and the host process's inherited
`PG*` connection settings are ignored. Container mode accepts only a loopback URL and verifies
that this endpoint and the pinned container reach the same database before
running PostgreSQL tools. Native tools support the configured cloud URL and TLS
options without a Docker connection.

For a running local Compose database, use the following in a POSIX shell. First
provision a separate empty recovery database and set `DATABASE_URL` and
`RESTORE_DATABASE_URL` to the source and recovery databases. Both URLs must reach
this container through its published loopback port and use different database names:

```sh
DB_CONTAINER="$(docker compose ps -q db)"
python -m scripts.backup_restore dump --archive work/notes.dump --db-container "$DB_CONTAINER"
python -m scripts.backup_restore restore --archive work/notes.dump --db-container "$DB_CONTAINER"
python -m scripts.backup_restore compare
```

The comparison is intended for a quiesced test dataset; it checks table
row counts and canonical row fingerprints without printing row contents. Actual
production recovery should additionally define recovery point/time objectives,
backup retention/encryption, and managed failover procedures.

## Verify

The source snapshot audited on November 15, 2026 passed
207 tests, including 41 focused migration checks, plus dependency validation,
Ruff, Pyright, and deprecation checks. The existing laptop database also restarted
successfully with unchanged note contents and row counts.

Run dependency validation, linting, type checking, and the full test suite using
Docker. No host Python installation, virtual environment, database creation, or
environment variable setup is required:

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
the pinned command line checkers provide reproducible CI results.

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
Pydantic models used at runtime. After starting the server, use the
[local API documentation](#local-api-documentation) to try requests in Swagger UI,
read the ReDoc reference, or obtain the OpenAPI document for client generation.
For protected requests, register an account, sign in through `/auth/login`, then
paste only the returned `access_token` into **Authorize**.
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
  ID tie breaker; teams and rosters sort by team/user ID ascending. Empty or
  exhausted pages return `[]`. An inaccessible parent resource still returns `404`.
- Note lists optionally accept `q`: a trimmed 1–200 character, case insensitive
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
  are documented as write only. Unexpected failures return a generic JSON `500`
  without database details, credentials, or tracebacks.
- Note updates require both editable fields and the latest `version` in the
  request body; deletion requires the version query parameter. A stale authorized
  write returns `409` without changing data. Read the current note before retrying.
  Read only sharing never grants update, deletion, or share management permission.
- Unsupported methods return `405` with an `Allow` header listing every supported
  method for that resource.
- Resource IDs are positive PostgreSQL BIGINT values. Timestamps are RFC 3339
  date and time strings with a UTC offset. Unknown JSON body fields are rejected;
  request limits and examples are included in the generated schema.

`tests/test_api_contract.py` validates the generated OpenAPI document, checks
operation/security/status/header conventions, and validates actual HTTP response
bodies against their published JSON schemas. A PostgreSQL backed flow checks
registration, login, creation, canonical retrieval, and logout. These checks run
with the regular Docker verification command and treat deprecations as errors.
OpenAPI and JSON Schema validators are development dependencies only.

## Project layout

```text
app/
  __init__.py
  api_contract.py   # Shared OpenAPI response models, headers, and conventions
  auth.py           # Authentication routes and request/response models
  config.py         # Validated pool, timeout, and deployment settings
  database.py       # Pooled ORM sessions and Alembic migration runner
  main.py           # Application, liveness, readiness, and metrics endpoints
  migration_compat.py # Frozen historical schema checks for one time adoption
  migrations/       # Versioned Alembic Python revisions
  models.py         # Typed SQLAlchemy database models
  notes.py          # Note CRUD, version checks, visibility, and search
  observability.py  # Bounded per process Prometheus metrics
  rate_limit.py     # Shared PostgreSQL login attempt budgets
  sharing.py        # Revocable read only recipient team grants
  security.py       # Password hashing and token helpers
  teams.py          # Team and owner managed membership routes
  validation.py     # Shared PostgreSQL text and password encoding validation
tests/
  test_api_contract.py # OpenAPI validation and HTTP/schema consistency
  test_auth.py      # Authentication HTTP integration tests
  test_teams.py     # Team authorization and membership integration tests
  test_database.py  # Connection and persistence behavior tests
  test_migrations.py # Migration behavior tests
  test_migration_history.py # Strict revision history and collision rejection
  test_notes.py     # Note creation/read access tests
  test_note_mutations.py # Versions, writes, deletion, and transaction races
  test_search.py    # Literal search, access filtering, and pagination
  test_sharing.py   # Read only grants and revocation
  test_acceptance.py # Full API collaboration and persistence scenarios
  test_database_config.py # Pools, timeouts, cloud role, rollout, and cleanup
  test_cursor_pagination.py # Stable pagination and permission changes
  test_operations.py # Readiness, metrics privacy, and resource failures
  test_rate_limit.py # Shared account/address throttling
  test_hash_capacity.py # Shared password hashing memory admission
  test_backup_tools.py # Safe backup/restore boundaries and credentials
  test_load_tools.py # Load tool input and scheduling behavior
  test_text_validation.py # Invalid text rejection and password round trips
scripts/
  demo.py           # Complete HTTP demonstration using the standard library
  migrate.py        # Separate cloud migration command
  cleanup_sessions.py # Bounded session and throttle window maintenance
  load_test.py      # Disposable two replica HTTP benchmark
  benchmark_data.py # Synthetic fixtures and connection/lock samples
  query_plans.py    # Actual PostgreSQL query plan diagnostics
  backup_restore.py # Private archives, guarded restore, and comparison
deploy/             # Kubernetes manifests, NGINX, and deployment guide
AGENTS.md           # Development instructions
Dockerfile          # Runtime and test container stages
compose.yaml        # API, PostgreSQL, and isolated test services
compose.scale.yaml  # Two replicas, gateway, and separate migrations
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

GitHub Actions runs the Docker only validation command, including dependency
compatibility, Ruff lint rules, Pyright types and deprecations, and PostgreSQL
integration tests. It then builds and starts the application stack, checks the
HTTP endpoints, verifies that the API container runs as a nonroot user, and runs
the complete HTTP collaboration demo. The workflow uses a pinned checkout action and read only repository permissions. No
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
   hashing, while random bearer tokens are stored only as `SHA-256` hashes. Sessions
   expire after one hour and logout revokes them immediately. This avoids JWT
   signing key management and delayed revocation, at the cost of a database lookup
   for each authenticated request and password hashing work during login.
3. **Membership filtered queries and versioned writes.** Private note access
   checks ownership; team note access checks current membership, including for the
   original creator. This makes membership removal take effect on subsequent reads without
   copying access lists onto each note. Filtering also happens before pagination,
   and missing and inaccessible notes share a `404` response. The tradeoff is more
   involved queries and a membership lookup for team access; these are covered by
   API tests against isolated PostgreSQL schemas. Saves and deletes lock the note,
   require the caller's current version, and check current membership in the same
   transaction. Conflicting saves return `409` instead of overwriting another
   writer's changes. Clients must reload and reconcile conflicts; this provides
   safe shared saves without simultaneous character level editing. Read only grants
   extend the read predicate without changing write permissions. Literal substring
   search shares that read predicate. Bounded offset pages preserve the original
   API, while caller/query bound cursor pages support deep continuation. Ordered
   and trigram indexes have measured query plans, with added storage and write
   maintenance as their tradeoff.

## What I would change, add, or stop with more time

With more time, I would prioritize:

- **Change:** Tune the implemented pool, timeout, throttle and index settings from
  sustained mixed workload measurements on the selected cloud/database tier.
- **Add:** Email verification, password reset, and a stronger account recovery flow
  before opening registration beyond the development environment.
- **Add:** Note revision history and an audit trail for content, sharing, and
  membership changes, beyond the current optimistic version counter.
- **Add:** Long duration soak, failover, and disaster recovery exercises with stated
  recovery point/time objectives, managed backup retention/encryption and alerts.
- **Keep:** Separate cloud migration jobs, explicit reviewed schema compatibility,
  readiness based routing, and per replica metrics. The laptop remains one command
  runnable; the optional two replica profile demonstrates the deployment pattern.
