# Durable paper-ledger cutover runbook

**Status: not approved for production cutover.** The adapter and schema must remain on the review branch until all checks are green and the owner explicitly approves applying the migration and changing Render configuration.

## Before touching production

1. Confirm the draft PR checks are green and review the focused PostgreSQL integration tests plus the full test suite.
2. Confirm the Supabase project region/availability and connection limits are suitable for the Render service. No paid upgrade is authorized by this runbook.
3. In Supabase Dashboard → Connect, obtain the **Session pooler** connection string for the project's PostgreSQL database. Do not use the transaction pooler: the adapter sets `search_path` on its persistent session and takes transaction-scoped advisory locks. Never paste the URL into chat, GitHub, source files, or logs.
4. Back up the existing Render SQLite ledger if any state needs to be preserved. The currently configured `/tmp/v4data/paper-ledger.sqlite3` is ephemeral; if Render has restarted, it may already be absent. Do not assume data exists.
5. If a SQLite ledger is present, run the checked-in import utility from a trusted environment against a disposable PostgreSQL database first. It validates source integrity, preserves the Emergency Stop row, refuses a non-empty destination, and verifies imported row counts. Inspect the output and independently compare the nine table counts.

## Production migration and cutover (requires explicit approval)

1. Apply `supabase/migrations/20261028010000_create_paper_ledger_schema.sql` to the intended Supabase project only after verifying the project ref in the dashboard. The migration creates the private `paper_ledger` schema and does not grant access to `anon`, `authenticated`, or `service_role`; the server connects through a trusted PostgreSQL connection string.
2. Verify all nine tables exist in schema `paper_ledger`, RLS is enabled, and the schema is not exposed through the Supabase Data API. Do not loosen grants to make a connection test pass.
3. In Render service `ai-auto-trader-v4-backend`, add `PAPER_DATABASE_URL` as a secret environment variable containing the Session pooler connection string. Do not remove `PAPER_DATABASE_PATH` yet; it remains the SQLite development/fallback setting only when the PostgreSQL variable is absent.
4. Keep the service paper-only. Deploy the reviewed branch only after confirming the intended branch/build source and any APK clients remain compatible.
5. Verify `/api/v1/status` reports `state_storage.durability = POSTGRESQL`, `state_storage.survives_restart = true`, `orders_authorized = false`, and the current Emergency Stop state. Verify `/api/v1/risk`, `/api/v1/orders`, `/api/v1/positions`, and `/api/v1/audit` work.
6. With no live trading enabled, create only a harmless paper-only test record in an approved test environment, restart the service, and confirm it remains present. Activate Emergency Stop in the test environment and confirm it remains active after restart; do not clear it without deliberate operator action.
7. If the database is unavailable, the API must fail closed (503 for ledger-backed endpoints) and must not silently fall back to a fresh SQLite ledger.

## Rollback rules

- Never roll back to an empty or stale SQLite file while claiming the same state is preserved.
- If startup, schema, connectivity, record counts, or Emergency Stop verification fails, keep new state-changing operations blocked, retain the PostgreSQL data for investigation, and stop the cutover.
- Do not enable exchange order placement. `orders_authorized` must remain false.
- Do not upgrade a paid plan or accept nonzero costs without separate explicit approval.

## Secrets handling

- Keep the PostgreSQL URL only in the Render secret environment variable or another trusted server-side secret store.
- Do not print connection strings, passwords, or complete exception objects that might contain them.
- Never place database credentials in Android, GitHub commits, issue comments, logs, or screenshots.
