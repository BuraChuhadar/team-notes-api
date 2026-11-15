# Deployment profiles

The default Compose file is the one-command laptop setup. The scale overlay adds
two API containers behind a local gateway and a separate migration service:

```sh
docker compose -f compose.yaml -f compose.scale.yaml up --build -d --wait
```

Use a separate Compose project and unused `API_PORT`/`POSTGRES_PORT` values when
keeping an existing development stack running. The gateway publishes only a
loopback port and does not expose `/metrics`. Metrics can be scraped on each
private API container. Its `X-Forwarded-For` header is overwritten with the actual
client address. Only these unpublished API containers trust forwarded headers
from the demonstration network. On a cloud platform, set `FORWARDED_ALLOW_IPS`
to the actual trusted ingress addresses or CIDRs; the default Uvicorn setting
trusts only `127.0.0.1`. Never accept arbitrary forwarded addresses on an exposed
API port, because doing so would allow spoofing the login address limit.

## Portable Kubernetes example

The manifests use an external PostgreSQL database. They do not create a managed
database, registry, cluster, or cloud account. Supply a PostgreSQL URL with TLS
settings appropriate to your provider and configure its high availability,
backups, connection limit, and restore policy separately.

1. Build and push the runtime image for the target platform. Update
   `kubernetes/kustomization.yaml` and the migration Job's image from
   `notes-api:local` to the same immutable registry tag or digest.
2. Create the `notes-api-credentials` Secret in your namespace with a runtime
   `DATABASE_URL` and a separate strong `METRICS_TOKEN`. Use a PostgreSQL role with
   data access and sequence usage, but no schema-creation privilege. Create a
   separate `notes-migration-credentials` Secret containing `DATABASE_URL` for
   the schema-owner/migration role. Keep actual values out of these files and
   source control. Apply `kubernetes/configmap.yaml`.
3. If `pg_trgm` is already installed, set `DB_TRIGRAM_SCHEMA` in the ConfigMap to
   that existing namespace. Otherwise the migration job creates it in
   `notes_extensions`; only the migration role needs database/schema CREATE
   privilege. Grant the runtime role usage of the extension schema as required
   by your provider and read/write access to the resulting application tables.
4. For a first deployment, apply `kubernetes/migrate-job.yaml`, then wait for the
   Job to complete before applying `kubectl apply -k deploy/kubernetes`. For
   subsequent releases, follow the compatibility sequence below. Recreate the
   completed migration Job with the new image for each release. The job does not
   use API request timeouts; it has its own bounded migration timeout.
5. Configure an ingress/load balancer and TLS for the Service. The optional
   `ingress.example.yaml` shows explicit public paths and keeps `/metrics`
   private. Configure the API's trusted proxy addresses for the chosen ingress.

### Database roles and grants

Use a dedicated application database and apply the provider's equivalent of the
following as its DBA. These are operational provisioning commands, not application
queries or migration files. Replace the example database and role names. Run
the commands while connected to the `notes` database. Supply passwords through
the provider's secret mechanism or interactive `psql` `\password` prompts.

```sql
CREATE ROLE notes_migrator LOGIN;
CREATE ROLE notes_app LOGIN;
\password notes_migrator
\password notes_app

GRANT CONNECT ON DATABASE notes TO notes_migrator, notes_app;
GRANT CREATE ON DATABASE notes TO notes_migrator;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO notes_migrator;
GRANT USAGE ON SCHEMA public TO notes_app;
ALTER ROLE notes_migrator IN DATABASE notes SET search_path = public;
ALTER ROLE notes_app IN DATABASE notes SET search_path = public;

ALTER DEFAULT PRIVILEGES FOR ROLE notes_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO notes_app;
ALTER DEFAULT PRIVILEGES FOR ROLE notes_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO notes_app;
```

The `FOR ROLE` value must be the role that actually creates tables in the
migration job, not merely a role it belongs to. Default privileges apply to
future objects created by that specific role. Revoke the default `PUBLIC` schema
creation privilege only in the dedicated application database after checking
other applications do not rely on it. The runtime role must not own the schema
or receive the migration role's membership.

Run the migration job using `notes_migrator`. **After its first successful run**,
apply these grants as the table owner or an authorized DBA for the objects it
created, using the actual extension schema from `DB_TRIGRAM_SCHEMA` in the final
statement:

```sql
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
  public.users, public.teams, public.memberships, public.notes,
  public.note_shares, public.sessions, public.login_throttles TO notes_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO notes_app;
GRANT SELECT ON TABLE public.alembic_version TO notes_app;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
  ON TABLE public.alembic_version FROM notes_app;
GRANT USAGE ON SCHEMA notes_extensions TO notes_app;
```

Readiness needs `SELECT` on `alembic_version`; the API does not need to modify
that ledger. Default table privileges may initially grant ledger writes, so the
explicit revocation is part of provisioning. Existing databases also need the
post-migration grants above; default privileges do not grant access retroactively.
Run an API readiness and registration/login/note CRUD smoke test with the runtime
credentials before exposing the Service. The integration suite repeats this
flow with a disposable, nonowner PostgreSQL login role and verifies that it has
no schema-creation or migration-ledger write permission.

Each API pod runs one Uvicorn worker and has a separate pool. `/health` checks
process liveness; `/ready` checks the database connection and accepted schema.
Cloud API pods use `AUTO_MIGRATE=false`, so replicas never run deployment DDL.
The CronJob removes expired sessions and old throttle windows in short batches.
The optional PodMonitor uses the metrics Secret and scrapes every pod separately;
it requires Prometheus Operator. Other collectors can use the same bearer header.

## Schema compatibility during rolling releases

Readiness accepts this image's expected Alembic head by default. A new, unknown
revision makes old images unready, even if the schema change is additive. To
keep old replicas available while an approved expansion is applied, set
`DB_COMPATIBLE_REVISIONS` to the explicitly reviewed future revision ID. The
default is empty. IDs are comma-separated, contain 1–32 letters, numbers, or
underscores, and are compared exactly; revision order does not imply
compatibility. An unexpected multiple-head database is rejected even if every
head appears in the allowlist. The allowlist changes readiness only; it does
not permit the old image's migration runner to apply unknown revisions.

The serving image must already implement this compatibility setting. An image
that predates this feature needs a code-only deployment at the unchanged schema
before this rolling-migration sequence can be used.

For example, when releasing a reviewed expansion from `006_login_throttle` to
`007_expand`:

1. Verify the actual new migration preserves old queries and writes. Adding a
   nullable column is an expansion; renaming or dropping a column used by old
   replicas is not. Run compatibility and rollback tests with both image versions.
2. Keep the **old image**, set `AUTO_MIGRATE=false` and
   `DB_COMPATIBLE_REVISIONS=007_expand` in its deployment configuration, and roll
   out that configuration. For Kubernetes environment variables from a ConfigMap,
   updating the ConfigMap alone does not update running pods: restart the old
   Deployment and wait for `kubectl rollout status deployment/notes-api` to
   confirm all old-image replicas are ready with the reviewed allowlist.
3. Run the new image's migration Job and wait for completion. Old replicas now
   accept `007_expand` while continuing to use their old models.
4. Roll out the new API image and wait for all new replicas to be ready.
5. Remove `007_expand` from the allowlist and roll out the cleared configuration.
   The new image accepts its own expected head without an exception.

The allowlist is an operator assertion, not an automatic schema compatibility
check. Breaking changes need separate expand/contract releases and a reviewed
rollback window, or a maintenance deployment with old replicas stopped. Never
add arbitrary future revisions to keep a failing deployment green.

The existing migration `005_query_indexes` builds its indexes inside
the migration transaction, without `CONCURRENTLY`. On a large existing table it
can block writes or exceed the migration timeout. Plan maintenance for that
upgrade or design a separate, reviewed concurrent-index migration strategy for
the production dataset. Do not change the already-applied revision or assume
that the initial migration profile provides arbitrary zero-downtime upgrades.

The database tests exercise a real temporary `007_expand` migration that adds a
nullable column: the old six-revision ORM can still read and update notes while
preserving the new column when that exact revision is allowed. Without the
explicit entry, readiness rejects the same database. This demonstrates the
release mechanism; each real migration still requires its own compatibility
review and tests.

For a rolling deployment, budget connections using every process that may still
hold a pool, including terminating pods that are draining requests:

```text
planned API budget = (replicas + surge + draining-pod allowance)
                     × workers × (DB_POOL_SIZE + DB_MAX_OVERFLOW)
```

The example has two desired replicas, one surge pod, and `5 + 5` connections per
single-worker pod. Its nonterminating-pod budget is 30 connections. Plan room for
two additional draining pods: `(2 + 1 + 2) × 1 × (5 + 5) = 50` API connections,
plus migration, cleanup, monitoring, and administrator reserves. This is a
conservative planning allowance for a serialized rollout, not a Kubernetes
hard connection cap. Kubernetes can retain terminating pods beyond
`replicas + maxSurge` during their graceful shutdown period. Recalculate the
allowance for overlapping rollouts, scaling, or a different drain duration, and
monitor actual PostgreSQL connections. Enforce a global cap with the database
or an external connection pooler when required. Keep the complete budget below
the database's configured limit. See the [Kubernetes deployment lifecycle](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/).

Container requests/limits in these manifests are initial development
defaults. Size them from the load-test measurements, including Argon2 CPU and
memory use. Functional tests and a small local benchmark do not prove a production
capacity or failover objective.

`PASSWORD_HASH_CONCURRENCY=2` is a shared per-worker admission limit for both
registration and login. Excess password operations return `503` with
`Retry-After: 1`. Argon2id uses approximately 64 MiB per operation before process
overhead, so include `workers × PASSWORD_HASH_CONCURRENCY × 64 MiB` in the pod's
memory budget. Keep one worker with two slots for the example's 512 MiB limit;
raising worker count also multiplies database pools and password-hashing memory.
