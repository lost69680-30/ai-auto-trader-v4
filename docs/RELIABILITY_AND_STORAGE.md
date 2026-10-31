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


## No-cost health checks and backup/restore preparation — 2026-10-31

### Health endpoints

The staging API now exposes two unauthenticated, deliberately minimal probes:
- `GET /healthz`: process liveness only. Returns a small PAPER_ONLY response and `orders_authorized=false`; it reveals no account, position, credential, or worker details.
- `GET /readyz`: readiness requires the shared market worker heartbeat to be `RUNNING`, the validated market feed to be `HEALTHY`, and the reported order-authorization flag to be exactly false. It returns HTTP 503 if those conditions are not met or the health lookup fails.

The probes are covered by tests in both source mirrors. The Step 3 market validation workflow passed on commit `2285343b983b8aea1ab34a1257c4517aabd97dc1`: **98 passed, 11 skipped**; the 11 skipped tests require a PostgreSQL integration database not provisioned in that CI job. The source-mirror comparison and static read-only safety checks also passed.

Render service settings currently report an empty Health Check Path. The Render connector available for this work can inspect the setting but cannot change it. Set the staging service Health Check Path to `/healthz` in Render Dashboard → staging service → Settings if desired. Do not use `/readyz` as the platform deploy health check: worker startup may take longer than API startup; use it for a separate readiness monitor.

### Backup and restore workflow

Two manual GitHub Actions workflows have been added on the staging branch:
- `.github/workflows/solo-staging-db-backup.yml` exports only the application-owned `paper_ledger` schema using a PostgreSQL custom-format archive, verifies the archive, calculates a SHA-256 checksum, and stores a GitHub Actions artifact for 7 days.
- `.github/workflows/solo-staging-db-restore-drill.yml` downloads a selected backup artifact and restores it into a disposable PostgreSQL 16 service container in GitHub Actions. It never connects to staging or production for the restore step.

The two workflows are prepared but **not yet runnable end-to-end** because the login credential has not been configured. A staging-only role `solo_backup_reader` has now been created with `NOLOGIN`, `NOINHERIT`, no superuser/BYPASSRLS attributes, CONNECT on the database, USAGE on `paper_ledger`, SELECT on all 14 current application tables and sequences, and default SELECT privileges for future objects created by `postgres`. A SELECT-only RLS policy was added for this role on each of the 14 current tables. Privilege checks confirmed SELECT on all 14 current application tables and zero INSERT, UPDATE, or DELETE privileges on any of them. The role has no login yet and does not have BYPASSRLS; its SELECT access comes only from 14 role-specific RLS policies. There are no routines in the `paper_ledger` schema. No other role was granted schema access. Because the tables have RLS enabled and no policies for ordinary roles, these role-specific SELECT policies are necessary for a complete dump; do not remove them while using this backup role.

The role is deliberately still `NOLOGIN`. To activate it, set a strong unique password directly in Supabase SQL Editor using `ALTER ROLE solo_backup_reader WITH LOGIN PASSWORD '<your-new-random-password>';` (replace the placeholder locally; never send the password in chat). Then add a GitHub repository Actions secret named `SOLO_BACKUP_DATABASE_URL` containing a connection URI for this role. Use the Supabase direct or session-pooler connection details from the project dashboard, target database `postgres`, and URL-encode special characters in the password. Do not use the existing owner-level application DSN. Never paste the DSN or password into chat, source files, workflow logs, or artifacts.

After the secret is configured: run the backup workflow manually from this staging branch; confirm its artifact exists; then run the restore-drill workflow manually and enter the backup workflow's run ID. A successful restore drill proves that the application schema archive can be restored into clean PostgreSQL 16. It does not prove recovery of Supabase platform roles, other schemas, secrets, or the full Supabase project. Download each successful artifact to independent user-controlled storage before its 7-day GitHub retention expires.

The workflows are intentionally manual. GitHub scheduled workflows execute from the repository's default branch; adding a scheduled backup there would modify the production branch and could trigger production deployment. No production change was made to enable scheduling.

### No-cost boundary

No Render plan, Supabase plan, database, or paid add-on was upgraded or created. No keep-alive polling was added to defeat Render Free suspension. Free hosting may still suspend; health probes and scheduled recovery checks do not create an always-on guarantee.


### Staging health-probe deployment result

The health-probe code was deployed to staging as Render deployment `dep-dbit5f142hec73beeoq0`, which reached `live`. During the rolling replacement, the new worker correctly refused to start while the old worker still held the PostgreSQL lease. The supervisor retried with bounded backoff; after the old process stopped and its lease was released/expired, the new worker started and resumed publishing. The post-recovery database check at 11:40:06 UTC observed worker status `RUNNING`, 10/10 healthy snapshots, all snapshot authorization flags false, 126 successful scheduler jobs, and zero paper orders or positions. This is evidence of one successful rolling-deploy overlap recovery, not proof of repeated fault injection or uninterrupted availability.

A manual GitHub Actions workflow `.github/workflows/solo-staging-health-check.yml` is also available on this staging branch. It checks `/healthz` and waits up to three minutes for `/readyz` to report a healthy shared worker. It is deliberately manual so it does not create keep-alive traffic or mask Render Free suspension. The workflow ran successfully on 2026-10-31 (GitHub Actions run `41198008877`): its live HTTP request to `/healthz` returned the expected PAPER_ONLY liveness payload, and `/readyz` returned ready with worker status `RUNNING`. Both checks verified `orders_authorized=false`. This is an actual end-to-end HTTP check from a GitHub-hosted runner, not merely a unit test.
