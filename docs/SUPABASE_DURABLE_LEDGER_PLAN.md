# Supabase PostgreSQL Durable Paper Ledger — Implementation Plan

## Objective

Replace the Render runtime's ephemeral `/tmp/v4data/paper-ledger.sqlite3` as the deployed backend's authoritative paper-trading state store with Supabase PostgreSQL. This is separate from the optional audit mirror: a mirror alone cannot restore orders, fills, positions, account-risk snapshots, or Emergency Stop state.

## Verified deployment facts

- Render service: `ai-auto-trader-v4-backend`
- Service branch: `main`
- Start command uses `PAPER_DATABASE_PATH=/tmp/v4data/paper-ledger.sqlite3`.
- Render build command installs `./extracted-source/ai-auto-trader-v4-assembly`; deployed changes must be made and tested in that package tree, not only in the top-level duplicate.
- The SQLite ledger defines nine tables: paper_orders, paper_fills, paper_positions, paper_funding_events, paper_audit_events, paper_system_controls, paper_order_cancellations, paper_position_extrema, and paper_equity_snapshots.
- The existing Supabase project currently has only `public.audit_events`; it does not yet contain a durable ledger schema.
- Render is currently on its Free plan. No paid plan, paid add-on, or nonzero charge is authorized.

## Design

1. Introduce a PostgreSQL-backed ledger implementation with the same public behavior as `SQLitePaperLedger`, using PostgreSQL transactions and constraints rather than translating SQLite SQL at runtime.
2. Keep SQLite for local tests, replay, and offline development. The deployed service selects PostgreSQL only when an explicit `PAPER_DATABASE_URL` is configured.
3. If PostgreSQL is selected but connection/schema/integrity checks fail, startup and state-changing endpoints must fail closed. Never silently fall back to an empty local SQLite database.
4. Store all nine ledger tables in a dedicated non-exposed schema, with least-privilege database access, constraints, indexes, and no grants to Supabase `anon` or `authenticated` roles. Enable RLS as defense in depth.
5. Keep Emergency Stop in the same PostgreSQL database and transaction domain as paper orders. The stop must be read before accepting a paper intent; database uncertainty blocks new intents and returns a clear unavailable/error state.
6. Preserve idempotency for intent IDs, fills, funding events, cancellations, and stop-state changes. Concurrency tests must prove duplicate submissions cannot create duplicate records.
7. Do not introduce exchange order placement. `orders_authorized` must remain hard-coded false throughout the ledger and API.
8. Use a server-only PostgreSQL connection string in Render environment secrets. Never store it in source, logs, the Android APK, or a client-facing endpoint.
9. Migrate existing SQLite data only through a separately tested, repeatable import utility that verifies row counts, primary keys, relationships, risk state, and Emergency Stop state before cutover. The first deployment must not silently discard current records.
10. Preserve the current deployment until tests, schema migration, and a reviewed cutover procedure are ready. Do not change Render environment variables or deploy automatically as part of this branch.

## Required tests before cutover

- Ledger contract parity for all public operations currently exercised by SQLite tests.
- Reconnect/restart test proving orders, fills, positions, risk snapshots, audit history, and Emergency Stop state survive.
- Parallel/concurrent duplicate intent, fill, funding, and stop tests.
- Transaction rollback and database timeout/unavailable tests.
- Schema/integrity mismatch fails startup; no ephemeral fallback.
- API status and Emergency Stop endpoints remain fail-closed when the database is unavailable.
- Existing SQLite test suite and all backend tests still pass.
- CI runs focused and full tests on the exact nested package installed by Render.
- A staging verification confirms `orders_authorized=false` and no exchange order endpoint exists.

## Cutover and rollback

- First deploy in paper-only mode with the PostgreSQL adapter disabled until a verified connection string and schema are available.
- Back up and import any SQLite data that must be retained before enabling PostgreSQL as source of truth.
- Verify row counts, state, and Emergency Stop state, then enable PostgreSQL through an explicitly reviewed Render environment change.
- If any verification fails, keep new paper intents blocked and restore the prior deployment/configuration only after determining which state is authoritative. Never automatically switch back to an empty SQLite database.

## Explicit non-goals

- No live trading or exchange order placement.
- No automatic paid-plan upgrade.
- No secret creation, reading, or posting in GitHub.
- No Render environment change or production deployment in this implementation branch.
- No claim of persistence until the database is connected and restart/concurrency tests pass.