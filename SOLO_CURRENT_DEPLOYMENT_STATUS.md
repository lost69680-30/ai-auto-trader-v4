# Solo Current Deployment Status — 2026-11-05

This is the current deployment-specific status. It supersedes older runtime descriptions in historical audit files; historical test results remain historical and must not be presented as current runtime health.

## Repository integration

- Default branch: `main`, current observed head `448960021cf669d397b736f5b10d7a33632ddaad`.
- Parts 1–8 integration was merged into `main` through PR #80. PR #80 is closed as merged.
- The integration branch head at the time of this check was `1d3eda70b16325a3d05aff1ede8427e75bdaebae`. It is four commits behind `main` but has zero file differences; the trees compare identical.
- The current `main` head includes a five-minute GitHub Actions Render keep-warm/readiness probe. It is explicitly best-effort, not an uptime guarantee.
- PR #124 fixed the worker's graceful-shutdown false failure and is merged. Regression tests were first observed failing (2 failed, 589 passed, 50 skipped); after the fix, all four checks passed: Python, PostgreSQL, ASGI, and read-only market-data safety. Root and packaged worker/test files were verified identical.
- PR #117's hosting/API contract work passed its hosting-resilience, Python, PostgreSQL, ASGI, read-only safety, and Android staging APK/unit-test checks before merge.
- No check runs were returned for the exact current `main` head SHA at the latest inspection. Do not imply that a fresh full CI run occurred on that exact SHA.

## Current Render staging topology

- Staging service: `solo-ai-auto-trader-staging-api`, Render Free, Frankfurt, auto-deploy enabled, tracking `feat/solo-ledger-dashboard-integration`.
- Start command launches the API/supervisor in the packaged assembly. The supervisor coordinates the API and shared market worker; the legacy separate worker service is not relied upon as a second active worker.
- Latest successful staging deployment: commit `1d3eda70b16325a3d05aff1ede8427e75bdaebae`, deployment `dep-dblvo9rncjis738ku580`, completed 2026-11-05 03:47:45 UTC.
- After deployment, logs showed successful market cycles, all ten configured symbols healthy, scheduler jobs completing, 10 new candles archived, historical archive caught up, and paper equity snapshots persisted with `orders_authorized=False`.
- **Latest runtime check at 2026-11-05 04:22:06 UTC did not show a current healthy worker.** The most recent database heartbeat was 04:02:35 UTC (1,172 seconds old); its lease expired at 04:04:05 UTC. The latest Render application logs also ended around 04:00 UTC. Treat the worker as stale/not currently verified, not RUNNING.
- The five-minute GitHub keep-warm workflow is a best-effort liveness/readiness probe. A scheduled workflow may be delayed, skipped, or fail; it cannot guarantee uninterrupted processing on Render Free. Never present this arrangement as guaranteed 24/7 hosting.

## Staging database and analysis evidence

- Isolated staging Supabase project: `solo-ai-auto-trader-staging` (project reference `gryjtjobkmredawmymwy`).
- Latest read-only query returned 4,100 market-intelligence reports across 10 symbols, latest assessment at 04:01:26 UTC. All report rows explicitly have `orders_authorized=false`.
- The opportunity registry currently contains zero persisted opportunity rows. No eligible opportunity should be invented or shown as available.
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
