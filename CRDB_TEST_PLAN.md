# LiteLLM on CockroachDB — Test Plan

Purpose: prove LiteLLM is production-viable on CockroachDB, focused on the surface CRDB actually
changes. This is not a general LiteLLM regression plan. We test three things: the five code changes
on this branch (which are concurrency-control rewrites and must be tested *under concurrency*, since
a single-threaded pass never exercises a lock), the raw-SQL paths the assessment reasoned were
compatible but we have not actually executed on CRDB, and the bootstrap/upgrade flow.

Scope note: what runs through Prisma model queries is provider-agnostic and low-risk. The risk lives
in raw SQL (`query_raw`/`execute_raw`), the ON CONFLICT spend counters under CRDB's isolation model,
and schema bootstrap. Prioritize accordingly.

## Test environment

Reusable harness (durable paths, survives /tmp cleanup):

- CRDB: `cockroach start-single-node --insecure --listen-addr=localhost:26257` (multi-node for the
  contention and node-failure cases, see P1-7)
- DB: `litellm`, schema applied from the generated CRDB DDL (see recommended path in
  CRDB_COMPATIBILITY_ASSESSMENT.md)
- venv: `~/.litellm-crdb/venv`, config: `~/.litellm-crdb/config.yaml`
- Boot: `DATABASE_URL=postgresql://root@localhost:26257/litellm?sslmode=disable`,
  `DISABLE_SCHEMA_UPDATE=true`, `PRISMA_SCHEMA_DISABLE_ADVISORY_LOCK=1`, master key set
- Isolation matrix: run the P0 and P1 suites twice, once with the database default at
  `read committed` and once at `serializable` (CRDB default). The delta is the production
  recommendation.

Every test records: pass/fail, any 40001 (`RETRY_SERIALIZABLE`) or 40P01 (`deadlock`) surfaced to
the client, and correctness of the resulting DB state (not just HTTP 200).

---

## P0 — must pass for any CRDB use (blocks the "it works" claim)

Each P0 that maps to one of our fixes must be run concurrently, not just once.

### P0-1 Bootstrap from empty
- Apply generated DDL to an empty database; assert all 77 tables present and every expected index
  exists (compare `SHOW CREATE` against the Prisma schema's `@@index`/`@unique`).
- Confirm the reporting views from `create_views.py` actually create on CRDB
  (`LiteLLM_VerificationTokenView`, `MonthlyGlobalSpend`, the Last30d* set, `DailyTagSpend`,
  `Last30dTopEndUsersSpend`). The assessment reasoned these work; this executes them.
- Reboot with `DISABLE_SCHEMA_UPDATE=true`; assert clean startup, `db: connected`, no schema errors.

### P0-2 Auth + key lifecycle
- Generate, read, update, regenerate, delete, block/unblock a virtual key; assert each persists.
- Key auth on the request path resolves through `LiteLLM_VerificationTokenView` (a real completion
  authenticated by a virtual key, not the master key).
- Model-access list on a key actually restricts which models it can call.

### P0-3 Request path across endpoints and providers
Each is a case; every one writes a spend log, so this also exercises the logging path.
- `/v1/chat/completions` non-streaming and streaming
- `/v1/messages` (Anthropic shape)
- `/v1/responses`
- `/v1/embeddings`
- Providers: Vertex (cloud) and Ollama (local) at minimum, to cross providers over the same DB path.

### P0-4 Spend log write + attribution
- After each request, assert a `LiteLLM_SpendLogs` row with correct model, non-zero tokens, and
  spend attributed to the right key/team/user.
- Assert the daily aggregate tables (`LiteLLM_DailySpend*`) receive the rolled-up upsert.

### P0-5 Team lifecycle (advisory-lock replacement, fix #3) — CONCURRENT
- Single-threaded: `/team/new`, `/team/member_add`, `/team/member_delete`, `/team/delete` (done).
- Concurrent: N parallel `/team/member_add` to the same team; assert every member lands exactly once
  and the final `members_with_roles` is correct (no lost update from the `FOR UPDATE` read).
- Concurrent `/team/member_add` on a team while `/access_group` update touches the same team; assert
  no deadlock (40P01) surfaces and both converge. This is the exact AB/BA scenario the row-lock
  ordering was designed to prevent.

### P0-6 Auto-router capability slot lock (fix #4a) — CONCURRENT
- Set a capability limit of 1. Fire two concurrent `/model/new` calls that each claim the gated
  capability; assert exactly one succeeds and one gets 403, and exactly one row is committed. Proves
  the `LiteLLM_Config` sentinel-row lock serializes the count-then-write.

### P0-7 MCP env-var merge lock (fix #4b) — CONCURRENT
- Concurrent `merge_user_env_vars` for the same `(user_id, server_id)` starting from no row; assert
  no lost update and the first-write seeding path works (the `INSERT ... ON CONFLICT ... RETURNING`).

### P0-8 Worker heartbeat (make_interval rewrite, fix #1)
- Assert the heartbeat upsert writes a row, `COUNT_SQL` returns the right live-worker count, and
  `PRUNE_SQL` deletes a row aged past the retention window (backdate a row and confirm prune). This
  is the SQL that failed every 60s before the fix.

### P0-9 Token metadata recovery (sha256-in-Python, fix #2)
- Exercise the CloudZero/Focus export path that calls `key_metadata_recovery` (keys with null alias
  that look double-hashed); assert alias/team/user are recovered, i.e. the Python `hash_token`
  digest matches the stored spend-log `api_key` on CRDB.

---

## P1 — production readiness (blocks the "run it in prod" claim)

### P1-1 Isolation-level decision (the headline CRDB risk)
- Run P0-4, P0-5, P0-6 under both `read committed` and `serializable`.
- Under serializable, measure how often the spend/counter upserts and the row-lock endpoints throw
  40001 to the client (Prisma does not auto-retry). Output: a clear recommendation (run READ
  COMMITTED, or add retry logic, or both).

### P1-2 Spend counters under load
- Hammer one key and one team with many parallel completions; assert final spend equals the sum of
  per-request costs (no lost increments), and record 40001 rate. This is the ON CONFLICT hot path.

### P1-3 Budget enforcement
- Key, team, and user `max_budget` each actually block requests once exceeded (429/budget error),
  and `budget_duration` window resets restore access. Enforcement must hold under concurrent spend.

### P1-4 Rate limits (tpm/rpm)
- Key/team tpm and rpm limits enforced correctly; note whether the deployment uses Redis or DB for
  the counter and test the DB-backed path.

### P1-5 Background jobs end to end
- `reset_budget_job`, spend-log cleanup/retention, and any DB-backed scheduled job run without SQL
  errors on CRDB and produce correct state.

### P1-6 Multi-worker topology
- Boot with N workers (gunicorn/uvicorn); assert heartbeat census is correct, spend under contention
  stays consistent, and no worker crashes on the shared DB.

### P1-7 Node failure / resilience
- On a multi-node CRDB, kill a node mid-load; assert the proxy recovers (Prisma reconnect) and no
  spend is lost or double-counted.

### P1-8 Upgrade path
- Populate CRDB on release N. Bump LiteLLM to N+1, regenerate the DDL diff, apply it; assert the
  migration only adds schema (no data rewrite), the app boots, and existing rows are intact.

---

## P2 — good to know (does not block adoption)

- P2-1 Confirm the cosmetic degradations from the assessment are harmless: `reltuples`-based row
  counts read 0 (UI stat), startup index-repair logs a swallowed warning, `pg_partitioned_table`
  probe returns empty and falls back to DELETE-based cleanup.
- P2-2 Confirm spend-log partitioning is cleanly unavailable (opt-in flag off by default) and does
  not error at boot; note CRDB row-level TTL as the substitute.
- P2-3 Analytics/admin UI queries that use DISTINCT ON, LATERAL, FILTER, jsonb SRFs, and parallel
  `unnest` actually return correct results on CRDB (Usage page, Logs page, spend-by-tag, session
  grouping). The assessment reasoned these are compatible; this executes them through the UI.
- P2-4 `sslmode=verify-full`/`sslrootcert` connection against CRDB Cloud (not just insecure local).

---

## Execution tracking

P0 is the gate for calling the branch functionally correct on CRDB. P1 is the gate for a production
recommendation. The concurrent cases (P0-5, P0-6, P0-7, P1-1, P1-2) are the ones that actually
validate the fixes, since the whole reason those code paths exist is contention. Automate the
concurrent cases as scripts (parallel curl or a small async client) so they are repeatable and can
move into `tests/e2e/` if we upstream.
