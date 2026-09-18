# LiteLLM × CockroachDB Compatibility Assessment

Date: 2026-09-14 · Branch: `CRDB-support` · Scope: LiteLLM proxy DB layer (Prisma + raw SQL + migration machinery)

## Summary

LiteLLM's proxy is much closer to CockroachDB-compatible than expected. The Prisma schema is
provider-portable (no `@db.*` native types, one `autoincrement()`), the runtime SQL is mostly
standard (ON CONFLICT upserts, DISTINCT ON, LATERAL, FILTER, jsonb functions, arrays — all
supported by CRDB), and there is **no LISTEN/NOTIFY, no triggers, no plpgsql, and no DEFERRABLE
constraints** in the runtime path.

The blockers cluster into two areas:

1. **Advisory locks** (`pg_advisory_xact_lock`, `pg_try_advisory_lock`, `hashtext()`) — not
   implemented in CRDB. Used in 4 places; the critical one guards core team-management endpoints.
2. **The shipped Prisma migration chain** — 14 of 174 migrations (including the baseline) wrap DDL
   in `DO $$ ... $$` PL/pgSQL blocks, which CRDB cannot execute. `prisma migrate deploy` is
   therefore not viable as-is; `prisma db push` is the workable path.

## Blockers (runtime SQL)

| # | Construct | Location | Impact | Fix direction |
|---|-----------|----------|--------|---------------|
| 1 | `pg_advisory_xact_lock(hashtext($1))` | `litellm/proxy/management_helpers/access_group_team_sync.py:30`, used by `team_endpoints.py:2820,3360,4115` | **Hard failure of `/team/member_add`, `/team/member_delete`, `/team/delete`** — core admin endpoints | Replace with `SELECT ... FOR UPDATE` on the team row or a dedicated lock table. Code comments (`team_repository.py:81-94`) document a lock-ordering rationale that must be preserved |
| 2 | `pg_advisory_xact_lock($1)` | `litellm/proxy/management_endpoints/model_management_endpoints.py:297` | Breaks model create/update when auto-router capability is used | Same: row lock / lock table |
| 3 | `pg_advisory_xact_lock($1::bigint)` | `litellm/proxy/_experimental/mcp_server/db.py:2062` | Breaks MCP per-user env-var writes (experimental) | Guards a single row — replace with `FOR UPDATE` |
| 4 | `make_interval(secs => $1)` | `litellm/proxy/db/proxy_worker_heartbeat.py:38-46` | Worker heartbeat fails every 60s per worker (non-fatal, log spam, breaks Admin-UI worker census) | One-liner: `NOW() - ($1 * INTERVAL '1 second')` |
| 5 | `encode(sha256(convert_to(token,'UTF8')),'hex')` | `litellm/proxy/spend_tracking/key_metadata_recovery.py:26-37` | Breaks spend-log key-metadata recovery (CRDB `sha256()` returns hex STRING, not bytea) | Use `sha256(token)` directly / dialect-gate |
| 6 | Native range partitioning of SpendLogs | `litellm/proxy/db/db_transaction_queue/spend_logs_partition_manager.py`, `db_scripts/partition_spend_logs.sql` | Opt-in feature unavailable; detection fails closed to DELETE-based cleanup (works) | Document as unsupported; CRDB row-level TTL is the natural substitute |

Degrades gracefully (no action needed): startup index repair in `litellm-proxy-extras`
(`pg_try_advisory_lock` + `REINDEX CONCURRENTLY` + `pg_table_size`) is fully wrapped in
`except psycopg.Error` → warning only; `reltuples`-based row-count stats read 0 (cosmetic);
`pg_partitioned_table` probe returns empty → correct fallback.

## Blockers (migrations)

- **14 migrations use `DO $$ ... END $$` blocks containing DDL** (FK add/drop guarded by
  `pg_constraint` lookups), including the baseline `20250326162113_baseline`. CRDB does not
  support DDL inside PL/pgSQL blocks → the shipped chain cannot apply.
- `20250509141545_use_big_int_for_daily_spend_tables` combines 7 `ALTER COLUMN ... SET DATA TYPE`
  subcommands per statement — CRDB requires one type change per statement, outside explicit txns.
- Prisma Migrate itself takes `pg_advisory_lock` on the `postgresql` provider before deploying —
  fails on CRDB. Escape hatch: `PRISMA_SCHEMA_DISABLE_ADVISORY_LOCK=1` (or the `cockroachdb`
  provider, which skips it).
- Everything else in the migration SQL is plain, CRDB-compatible DDL (no triggers, functions,
  extensions, DEFERRABLE, partial indexes). `CREATE INDEX CONCURRENTLY` parses fine.

## Recommended path (validated end-to-end 2026-09-14)

Empirical correction from the live boot test: **`prisma db push` with `provider = "postgresql"`
is refused outright** — Prisma's schema engine detects CockroachDB and errors with "Please change
it to `cockroachdb`". Only the schema engine checks; the *query engine* (what the proxy uses at
runtime) connects and operates against CRDB without complaint. So schema creation happens out of
band, once, and the proxy runs with schema updates disabled:

1. Make a copy of `schema.prisma` with `provider = "cockroachdb"` and
   `@default(autoincrement())` replaced by `@default(sequence())` (the cockroachdb provider
   rejects autoincrement on `Int`; `LiteLLM_ModelTable.id`, `schema.prisma:109`).
2. Generate native CRDB DDL:
   `prisma migrate diff --from-empty --to-schema-datamodel schema-crdb.prisma --script > crdb-schema.sql`
   (produces ~2000 lines, 77 tables, STRING/FLOAT8 native types).
3. Apply it: `cockroach sql -d litellm -f crdb-schema.sql`.
4. Boot the proxy with `DISABLE_SCHEMA_UPDATE=true` (skips push entirely; the drift check is
   log-only) and `DATABASE_URL` pointing at CRDB. `PRISMA_SCHEMA_DISABLE_ADVISORY_LOCK=1` is
   belt-and-suspenders for any schema-engine invocation.
5. Run the cluster (or role defaults) at READ COMMITTED (v23.2+) pending contention testing of
   the spend counters under SERIALIZABLE.

Verified working on CockroachDB v26.2 single-node with this setup plus the five runtime fixes on
this branch: proxy boot and readiness (`db: connected`), key generation, model creation via
`/model/new` (stored in DB), a real Vertex AI completion through a virtual key, spend-log write
and per-key spend attribution, worker heartbeat (1 live worker, rewritten interval SQL), and the
full team lifecycle (`/team/new`, `/team/member_add`, `/team/member_delete`, `/team/delete`) that
the advisory locks previously hard-blocked. No serialization-retry (40001) errors observed.

**Alternative (`provider = "cockroachdb"` everywhere):** cleaner engine behavior and would
restore `db push` for ongoing upgrades, but requires editing all three identical `schema.prisma`
copies (repo root, `litellm/proxy/`, `litellm-proxy-extras/litellm_proxy_extras/`) plus
`migration_lock.toml`, the `sequence()` change, rebuilding images (`prisma generate` runs at
Docker build time), and abandoning the shipped migration directory. Effectively a packaging fork;
revisit if the out-of-band DDL flow proves annoying for upgrades.

**Not viable:** `prisma migrate deploy` against CRDB without rewriting the 14 DO-block migrations
and the bigint migration; `prisma db push` with the postgresql provider (schema-engine refusal
above).

## Nice-to-haves already working in LiteLLM's favor

- `translate_libpq_ssl_params` handles `sslmode=verify-full&sslrootcert=...` (the CRDB Cloud
  connection-string style) automatically, rewriting to Prisma's dialect.
- Both `postgres://` and `postgresql://` schemes accepted.
- Healthchecks are `SELECT 1`; no server version-string parsing anywhere.
- All hot-path per-request DB work goes through Prisma model queries and ON CONFLICT background
  upserts — fully CRDB-compatible.

## Work status

Done on this branch (all portable SQL, identical behavior on Postgres, with regression tests):

1. `make_interval` rewritten to `NOW() - ($1 * INTERVAL '1 second')` in
   `proxy_worker_heartbeat.py`.
2. Token digests computed in Python (`hash_token`) instead of `encode(sha256(...))` SQL in
   `key_metadata_recovery.py`.
3. Team-sync advisory lock replaced with `FOR UPDATE` row locks under a documented global lock
   order (team rows first, sorted; access-group rows after), with the access-group endpoints
   updated to respect it. One disclosed semantic gap: a deleted team's post-commit detach pass
   is unserialized against an immediate same-id re-create (transient, self-healing mirror).
4. Auto-router capability slot lock replaced with a sentinel-row lock in `LiteLLM_Config`; MCP
   env-var merge lock replaced with a locking `INSERT ... ON CONFLICT ... RETURNING`.
5. End-to-end validation against CRDB v26.2 (see recommended path above).

Remaining / follow-up:

- Contention testing of spend counters under SERIALIZABLE (currently sidestepped via READ
  COMMITTED); Prisma surfaces 40001s as errors, not retries.
- Run the real-Postgres serialization test (`tests/proxy_admin_ui_tests/test_access_group_team_sync.py`)
  against both Postgres and CRDB in CI.
- Upgrade story: each LiteLLM release needs a regenerated CRDB DDL diff (out-of-band) until/unless
  the provider is switched to cockroachdb.
- Decide upstream strategy: the five fixes are portable and individually upstreamable.
