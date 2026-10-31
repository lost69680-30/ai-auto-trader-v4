# Solo V4 — Reliability and storage status

_Last verified: 2026-10-31 (staging); see evidence and limitations below._

## Safety posture

Solo remains paper-only for development and staging verification. The staging market feed, market archive, scheduled analysis jobs, and stored candle records report `orders_authorized = false`. No exchange order-placement route is part of the read-only API. The independent risk gate remains fail-closed.

Do not enable live trading as part of reliability work.

## Staging storage: PostgreSQL is active

The Render staging service is configured with both:
- `PAPER_DATABASE_URL` (secret; never print or commit its value), and
- `PAPER_DATABASE_PATH=/tmp/v4data/paper-ledger.sqlite3` as a local SQLite fallback path.

The deployed API explicitly selects PostgreSQL when a non-empty `PAPER_DATABASE_URL` is present, and refuses to fall back to SQLite when the variable is present but empty. When the variable is absent, the API selects the configured SQLite path. The path alone must therefore not be used to infer the active database.

Staging logs on 2026-10-31 confirmed:
- "Paper ledger uses PostgreSQL and startup integrity checks passed."
- "API market feed reads durable PostgreSQL snapshots from the independent worker."

A read-only staging database query observed approximately 432,077 archived candles, 10 healthy market-feed snapshots, one worker heartbeat row, and the durable scheduled-job tables. The 10 market snapshots and candle rows checked in that query all had `orders_authorized = false`. The paper order and position tables were empty at the time of inspection.

The Supabase `paper_ledger` schema is private to direct server-side PostgreSQL access: `anon`, `authenticated`, and `service_role` do not have schema USAGE, and the tables have row-level security enabled. Do not grant Data API access merely to make client-side queries work.

## Worker, scheduler, and hosting limitations

The staging Render web service is on the Free compute plan. Its start command includes a SQLite fallback path, but the current staging runtime uses PostgreSQL as described above.

On 2026-10-31, Render logs showed the supervisor receiving SIGTERM and gracefully stopping both API and market-worker child processes at 11:07:29 UTC, about 15 minutes after the staging deploy became live. The worker's last recorded heartbeat was 11:07:24 UTC and its lease expired at 11:08:54 UTC. A subsequent database query showed three read-only market-feed audit jobs still pending.

This timing is consistent with Render Free web-service idle spin-down, not proof of an application crash. Render documents that Free web services spin down after 15 minutes without inbound HTTP traffic or inbound WebSocket messages; the next request or new WebSocket connection wakes the service and may take about one minute. A background worker running inside the same web service is stopped when the host spins down. A persistent database does not keep the compute process running.

The current Render service has no configured HTTP health-check path. Render's default TCP port check is not an application-level readiness check. The Render metrics query returned memory observations but no CPU or HTTP request series for the inspected window; monitoring evidence is incomplete.

The worker and API are separate child processes under a supervisor that restarts either child independently. Automated tests cover each child failing and restarting without restarting the other. The supervisor source and test file on the staging branch have the same Git blob SHAs as the branch used for the 2026-10-31 successful CI runs. No live fault-injection test was performed against the staging service.

The durable scheduler uses PostgreSQL-backed recurring schedule definitions, idempotency keys, bounded catch-up, leases, retry handling, and owner checks. On 2026-10-31, a successful CI run on the parallel `design/solo-hybrid-glass-ui` branch applied the same scheduler migration to a disposable PostgreSQL 16 service and ran the backend suite: 456 passed, 0 skipped. The scheduler, supervisor, dispatcher, scheduler-test, and migration files had identical Git blob SHAs on that branch and the deployed staging branch. A second durable-ledger run also passed its 6 PostgreSQL ledger integration tests. This verifies the automated DB-backed test path for the same source, but is not a live outage/restart test. The live staging backlog must still be rechecked after the service wakes; the last observed three pending jobs were not verified as drained after spin-down.

## Backups and restore

The Supabase organization hosting staging is on the Free plan. Supabase's current Free-plan guidance states that automatic daily database backups are not included; operators must perform manual exports (for example, with the Supabase CLI) and retain off-site copies.

No verified backup export or restore drill is recorded in this repository or in the staging checks summarized above. Therefore, PostgreSQL persistence across Render restarts is verified, but disaster-recovery backup/restore is **not** verified. Do not describe the database as fully backed up or disaster-recoverable until a dated export is retained off-site and restored into a separate test project/database successfully.

## Production boundary

Production was not changed as part of this staging reliability verification. The production Render service remains on its existing `main` deployment and has not been promoted to the staging shared-market-data/scheduler migrations. Do not apply staging migrations to production or promote the staging branch without a separate reviewed migration and compatibility plan.

## Part 1 verification status

Verified:
1. Staging selects PostgreSQL when `PAPER_DATABASE_URL` is configured.
2. Startup integrity checks passed.
3. API reads shared PostgreSQL market snapshots from the independent worker.
4. Market snapshots and archived candles were being persisted.
5. The scheduler's durable tables, recurring schedule, and idempotent job records exist.
6. CI passed on the staging commit for backend/Android contract tests (440 passed, 11 skipped), read-only market-data/safety tests, and offline backtest safety tests. Separate successful CI runs against identical scheduler/supervisor source and test files on the parallel design branch ran PostgreSQL-backed integration tests (including the scheduler migration) and passed.
7. The Render host shutdown was graceful, and the worker lease expiry was visible in PostgreSQL.

Not yet verified / remains an operational blocker:
1. Always-on API, WebSocket, and background-worker availability on the current Render Free plan.
2. Live post-wake worker heartbeat and pending-job drainage after idle spin-down.
3. A live API-child and worker-child fault-injection/recovery test (only automated unit/simulation tests have passed).
4. HTTP application health checks and complete CPU/HTTP monitoring.
5. A manual off-site database backup and successful restore drill.

Do not mark Part 1 fully production-ready until these open checks are closed. Keep staging paper-only and keep production untouched until the separate migration, hosting, and recovery plan has been reviewed.


## Post-deploy recovery recheck — 2026-10-31 11:28 UTC

The documentation-only staging deployment `dep-dbisrqnlk1mc73fl00n0` completed successfully and Render reported it as `live`. Startup logs showed the API process completed startup and the read-only WebSocket endpoint was enabled. The process log also explicitly identified the API-only role; the shared worker's activity was therefore verified from its durable PostgreSQL heartbeat and fresh snapshots rather than inferred from the API startup message.

A read-only PostgreSQL recheck after this deploy observed:
- `solo-market-worker` heartbeat status `RUNNING`, with heartbeat at 11:28:02 UTC and lease expiry at 11:29:32 UTC.
- All 10 configured market symbols had `HEALTHY` snapshots checked at 11:27:39 UTC.
- 432,287 archived candle rows; latest archive timestamp 11:28:16 UTC; all inspected archive rows retained `orders_authorized=false`.
- 124 scheduled jobs in `SUCCEEDED` state and no other job statuses in the grouped result. This closes the earlier three-job backlog for this recovery check.
- Emergency Stop was inactive; this is a state observation, not an instruction to enable execution.

Render metrics now returned sparse CPU samples and memory samples of approximately 120–154 MB for recent instances, but no HTTP request or latency series. The service configuration still shows the Free plan, one web service, and an empty health-check path. This confirms one successful cold-start/recovery cycle; it does not establish continuous uptime, repeated fault recovery, or application-level readiness monitoring.

## Updated Part 1 status

The post-deploy check closes the specific post-wake heartbeat and scheduler-backlog verification for this staging restart. The remaining blockers are: (1) continuous compute without Free-plan idle suspension, (2) a live fault-injection test of API and worker child recovery, (3) an unauthenticated application liveness/readiness endpoint wired into Render's health-check setting and useful HTTP metrics, and (4) a retained off-site database export followed by a successful restore drill. No production service, database, or configuration was changed.
