# Solo Current Deployment Status — 2026-11-05

This is the current deployment-specific status. It supersedes older runtime descriptions in historical audit files; historical test results remain historical and must not be presented as current runtime health.

## Repository integration

- Default branch: `main`, current observed head `448960021cf669d397b736f5b10d7a33632ddaad`.
- Parts 1–8 integration was merged into `main` through PR #80. PR #80 is closed as merged.
- PR #127 has now merged the mainline keep-warm workflow and current deployment-status documentation into the Render integration branch. `main` and `feat/solo-ledger-dashboard-integration` have identical file trees; the integration branch is one merge commit ahead of main, with zero file differences and no application-source divergence.
- The current `main` head includes a five-minute GitHub Actions Render keep-warm/readiness probe. It is explicitly best-effort, not an uptime guarantee.
- PR #124 fixed the worker's graceful-shutdown false failure and is merged. Regression tests were first observed failing (2 failed, 589 passed, 50 skipped); after the fix, all four checks passed: Python, PostgreSQL, ASGI, and read-only market-data safety. Root and packaged worker/test files were verified identical.
- PR #117's hosting/API contract work passed its hosting-resilience, Python, PostgreSQL, ASGI, read-only safety, and Android staging APK/unit-test checks before merge.
- No check runs were returned for the exact current `main` head SHA at the latest inspection. Do not imply that a fresh full CI run occurred on that exact SHA.

## Current Render staging topology

- Staging service: `solo-ai-auto-trader-staging-api`, Render Free, Frankfurt, auto-deploy enabled, tracking `feat/solo-ledger-dashboard-integration`.
- Start command launches the API/supervisor in the packaged assembly. The supervisor coordinates the API and shared market worker; the legacy separate worker service is not relied upon as a second active worker.
- Latest successful staging deployment: commit `e740ecdca08d614f869ddac45803d8fc6d9a9d15`, deployment `dep-dbm0ar67bikc73eddghg`, completed 2026-11-05 04:27:15 UTC. It includes only the mainline keep-warm workflow and synchronized documentation; application source and safety behavior were unchanged.
- The deployment handover respected the PostgreSQL worker lease: the new worker refused duplicate polling while the old 90-second lease remained active, retried with backoff, then acquired the lease after expiry. It resumed its first complete analysis cycle at 04:29:33 UTC. This produced a short readiness gap but did not permit duplicate market polling.
- **Recovery re-verified at 04:30:11 UTC:** latest heartbeat 04:30:03 UTC (8 seconds old), lease valid until 04:31:33 UTC, status `RUNNING`, analysis `ANALYZED`, all ten configured symbols healthy, no unhealthy symbols, no analysis/equity snapshot error, scheduled-job runner `RUNNING`, historical archive caught up, and `orders_authorized=false`.
- Earlier, at 04:22:06 UTC, the worker heartbeat was stale and its lease had expired; a manual redeploy restored it. The current check confirms recovery and repeated cycles after restart, not uninterrupted multi-hour uptime.
- The five-minute GitHub keep-warm workflow is a best-effort liveness/readiness probe. Actual scheduled-run continuity has not been independently verified, and a scheduled workflow may be delayed, skipped, or fail. It cannot guarantee uninterrupted processing on Render Free. Never present this arrangement as guaranteed 24/7 hosting.

## Staging database and analysis evidence

- Isolated staging Supabase project: `solo-ai-auto-trader-staging` (project reference `gryjtjobkmredawmymwy`).
- Latest read-only query returned 4,130 market-intelligence reports across 10 symbols, latest assessment at 04:29:00 UTC. All report rows explicitly have `orders_authorized=false`.
- The opportunity registry contained zero persisted opportunity rows at the latest direct count. No eligible opportunity should be invented or shown as available.
- Futures candle archive query returned 499,827 one-minute candles for `BINANCE_USDM_TESTNET/FUTURES`, latest close at 04:01:59 UTC.
- Latest ledger query returned zero paper order records and zero open positions.
- Latest heartbeat had `analysis_status=ANALYZED`, no analysis or equity snapshot error, and `orders_authorized=false`; however, that heartbeat is stale as described above.
- The staging schema had previously been verified with 21 required tables and RLS enabled on all 21; this was an earlier schema/security check, not a substitute for a fresh production migration review.

## Android and notification acceptance

Previously user-confirmed device coverage:
- Android app installed and dashboard opened.
- Live market prices and connected stream displayed.
- Charts displayed across symbols/timeframes.
- Stream automatically recovered after an approximately ten-second internet interruption.
- Android notification permission was enabled.

Still not accepted on a physical device:
- A genuine backend risk alert delivered while the app is backgrounded.
- Delivery retry after notification permission/channel is restored.
- User acknowledgement and authoritative backend resolution.
- Background restart/recovery and duplicate suppression under real device lifecycle conditions.

The local notification test and Android CI build do not prove these physical-device behaviors.
- The background risk poll uses Android WorkManager with a 15-minute periodic interval. Android Doze, battery restrictions and scheduler constraints can delay it further; it is not an instant push-notification guarantee.

## Safety and release boundary

- Paper-only/read-only operation remains mandatory: `PAPER_ONLY`, `orders_authorized=False`, no live orders.
- AI agents, RL/PPO, GAT and other research components cannot bypass the independent Risk Guardian or call order endpoints directly.
- Current staging ledger reports zero paper orders and zero open positions; this is not proof of profitable trading.
- No production deployment or production database migration has been performed.
- Cloudflare/Deno secondary fallback source is tested but has not been deployed or provisioned with secrets.
- No paid plan, payment card, paid hosting resource, or billing change was initiated.

## Remaining acceptance blockers

1. Recover and verify a fresh staging worker heartbeat after the observed Render Free sleep/expiry; confirm readiness and repeated cycles rather than a single startup.
2. Verify the GitHub keep-warm workflow actually runs on schedule and reports readiness. Even if it does, retain the no-uptime-guarantee statement.
3. Complete genuine physical Android background-alert, acknowledgement, permission recovery and resolution acceptance.
4. Run a full CI validation on the exact final mainline release candidate before any production release.
5. Complete production migration/deployment only after an explicit production plan and safe backup/rollback verification. Do not silently apply staging migrations to production.

## Cost policy

Keep the $0/month ceiling. Do not enable paid hosting, trials, upgrades, billing verification or card-backed services. If a requirement cannot be met under free-tier limits, document it as a limitation rather than disguising it as complete.
