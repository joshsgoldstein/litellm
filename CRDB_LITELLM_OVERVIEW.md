# CockroachDB + LiteLLM: What We're Building

Internal overview for sharing with colleagues. For the technical deep dives, see
`CRDB_COMPATIBILITY_ASSESSMENT.md` and `CRDB_TEST_PLAN.md`.

## Context

LiteLLM is a widely used open-source LLM gateway. Teams put it in front of many model providers
(OpenAI, Anthropic, Bedrock, Vertex, local models, and so on) to get one API, one set of keys,
central spend tracking, budgets, and rate limits. Its proxy keeps all of that operational state in
a SQL database, and today that database is Postgres only.

We are making CockroachDB work with LiteLLM in three independent ways. They are separate pieces of
work and separate contributions; none depends on the others

## The three additions

### 1. CockroachDB as the backing database (the "spine")

**What:** Let LiteLLM's proxy use CockroachDB as its operational store, the database that holds API
keys, teams, users, budgets, and spend logs, in place of Postgres

**Why it matters:** This is the big one. It lets an organization run LiteLLM on CockroachDB and get
CRDB's resilience, scale, and multi-region behavior for their AI gateway's control plane

**How it works:** LiteLLM talks to its database through Prisma, and most of its schema and queries
are already portable. A small number of Postgres-only constructs were not. We replaced five of them
with standard equivalents that behave identically on Postgres, so this is portability work, not a
CockroachDB-specific fork. In plain terms the five changes are:

- Rewrote a time-window calculation in the worker health check to use standard SQL
- Moved a key-hashing step out of SQL and into application code
- Replaced three uses of Postgres "advisory locks" (a Postgres-only locking trick) with standard
  row-level locking, which is more precise and works everywhere

**Status:** Code complete, all lint and type checks pass, unit tests pass. Validated end to end on a
live CockroachDB: the proxy boots, creates keys, serves real model completions (Vertex and a local
model), records spend correctly, and runs the full team-management flow. Ready for the integration
team to test

**Isolation level:** Runs on CockroachDB's default (SERIALIZABLE). No configuration change is
required. The one thing the integration team is measuring is how often, under heavy concurrent
admin writes, a transaction returns a normal retryable conflict, which we expect to be rare and, if
it ever shows up, is a small targeted fix rather than a redesign

**Where:** branch `litellm_crdb_backend`

### 2. CockroachDB in the MCP connector list

**What:** LiteLLM's admin UI has a picker of well-known connectors when you register an MCP (Model
Context Protocol) server. We add CockroachDB to that list

**Why it matters:** Discoverability and first-class presence. It puts CockroachDB alongside the
other recognized names (PostgreSQL, Snowflake, and so on) so users see it as a supported option

**How it works:** A small UI addition (a logo and list entry). It is backend-agnostic and has
nothing to do with which database LiteLLM runs on

**Status:** Small, planned

**Where:** its own branch (to come)

### 3. CockroachDB as a vector store

**What:** LiteLLM can search external vector stores to power retrieval (RAG). We add CockroachDB as
a supported vector-store provider, using CockroachDB's native vector search

**Why it matters:** It lets people use CockroachDB's vector capabilities through the same LiteLLM
gateway they already use for model calls

**Important design point:** This is fully decoupled from addition 1. Someone can run LiteLLM on
Postgres (or anything) as its spine and still use CockroachDB purely as a vector store. The vector
store connects with its own credentials and never touches the proxy's main database connection

**Status:** Scoped. One design decision is open: connect through a small companion service (mirrors
how LiteLLM's existing Postgres vector store works, smaller change) versus connect directly to
CockroachDB over SQL (more valuable, larger change). We will confirm the direction before building

**Where:** its own branch (to come)

## How they fit together

They are independent. Each ships as its own pull request to the LiteLLM open-source project, so they
can be reviewed and merged on their own timelines. Addition 1 is the substantial one; additions 2
and 3 are smaller and self-contained. Keeping them separate also means the vector store (3) never
implies you must run CockroachDB as your spine (1)

## Contribution approach

We are contributing these upstream to the LiteLLM project (BerriAI/litellm) rather than maintaining
a private fork, so the work lands in the product everyone uses. That means following their
contribution rules: one focused change per pull request, tests, passing their full lint and type
gates, and real end-to-end proof of each change. A corporate Contributor License Agreement is in
progress, which is the one legal prerequisite before anything can merge

## Current status at a glance

- Addition 1 (backend): code complete and validated end to end, in integration testing
- Addition 2 (MCP connector): planned, small
- Addition 3 (vector store): scoped, one design decision open

## Deeper reading

- `CRDB_COMPATIBILITY_ASSESSMENT.md`: the full technical survey of what did and did not work on
  CockroachDB, and why
- `CRDB_TEST_PLAN.md`: the prioritized test plan the integration team follows, including the
  concurrency and isolation cases
