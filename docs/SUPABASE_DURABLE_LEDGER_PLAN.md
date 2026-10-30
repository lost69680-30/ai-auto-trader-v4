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

## Implementation status on the feature branch

The following implementation is present on feature/supabase-durable-ledger:
- A PostgreSQL adapter for the existing paper ledger API and SQLite query behavior, with startup schema checks, fail-closed behavior when PAPER_DATABASE_URL is set but invalid/unavailable, and a transaction-scoped advisory lock for serialized write operations.
- A private paper_ledger schema migration with all nine state tables, constraints, indexes, RLS enabled, and no grants through the public API roles.
- A one-time importer invoked as python -m app.engines.sqlite_to_postgres_import --sqlite-path /path/to/paper-ledger.sqlite3. It reads the source ledger read-only, verifies SQLite integrity and foreign keys, refuses a populated target (except for the default inactive Emergency Stop seed), imports in a single PostgreSQL transaction, preserves identity values and Emergency Stop state, checks row counts, and rolls back on errors.
- Remote PostgreSQL connections are upgraded to TLS (sslmode=require) unless a stronger verification mode is already configured. Local disposable test databases are exempted.
- A PostgreSQL CI workflow uses an isolated disposable PostgreSQL service and applies the migration only to that test database.

## Test evidence

The disposable PostgreSQL workflow passed 313 tests on commit 509e27c1a0b729c1a0a44f28bc924b89e6c55c03. Subsequent root/package parity and root TLS-contract changes have their own CI run in progress. Do not interpret this as validation against the actual Supabase instance or as a production deployment.

## Required production setup and cutover steps

1. In the Supabase dashboard, open Connect and choose the Session pooler connection method. On the Free plan, the shared session pooler is IPv4-accessible; its port is normally 5432. Do not use the transaction pooler (6543) because the adapter relies on session state and transaction-scoped locking.
2. Store the connection string as a Render secret named PAPER_DATABASE_URL. Do not put it into GitHub, the APK, logs, or this chat. It must use SSL; the code enforces TLS for remote hosts unless a stronger mode is selected.
3. Apply supabase/migrations/20261029205318_create_paper_ledger_schema.sql to the intended Supabase project only after reviewing the SQL and backing up any data that matters. This migration is applied only to the staging Supabase project; production remains untouched.
4. Before any cutover, obtain a consistent copy of the old SQLite file and stop writes to it. Do not import an actively changing database. Run the importer only after the migration has created paper_ledger; the importer intentionally refuses a non-empty destination.
5. Configure PAPER_DATABASE_URL on Render only after the database schema, secret, copy/import, and restore checks are reviewed. Verify startup integrity, row counts, API status, Emergency Stop persistence after restart, and orders_authorized=false before considering the cutover complete.

The Render service currently remains on main with PAPER_DATABASE_PATH=/tmp/v4data/paper-ledger.sqlite3. This branch has not updated Render settings, applied the real Supabase migration, imported user data, merged the branch, or deployed to production.
