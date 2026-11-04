# Solo zero-cost resilient hosting — implementation and operating contract

**Status:** source implementation and automated contract tests are prepared. Provider deployment and Android endpoint cutover remain gated on provider account access, assigned deployment URLs, and staging acceptance. This document does not claim the Cloudflare or Deno deployments are live.

## 1. Target topology

- **Render primary:** keep the existing `solo-ai-auto-trader-staging-api` as the authoritative Python API and authenticated WebSocket origin. Keep the separate `solo-futures-websocket-staging` service for its existing worker/supervisor role. Both are currently configured on Render Free.
- **Cloudflare Workers edge gateway:** `hosting/cloudflare/src/index.js` is the Android HTTPS/WebSocket entry point. It forwards the allowlisted API reads and the authenticated market WebSocket to Render. It never stores the user's API bearer token.
- **Deno Deploy secondary market source:** `hosting/deno/main.js` fetches public Binance USDⓈ-M Futures Testnet premium-index snapshots on demand. It has no database, credentials, account access, order routes, or write capability.
- **Supabase PostgreSQL:** remains the single authoritative paper ledger and audit database. No second database, multi-master writes, or cross-provider ledger replication is introduced.
- **GitHub Actions:** runs source checks and failover contract tests. Existing encrypted backup/restore workflows remain the database recovery mechanism.

## 2. Failover contract

### Primary-first behavior

- `GET /healthz` reports only that the edge Worker is alive; it does not claim Render or the database are healthy.
- `GET /readyz` checks the Render `/readyz` and Deno `/healthz` endpoints. It reports `ready` only when Render is ready; a healthy Deno fallback with a failed Render primary is reported as `degraded` and HTTP 503.
- Authenticated `GET /api/v1/status` and `GET /api/v1/markets` use Render first.
- If Render is unavailable or its market snapshot is invalid/stale, and the primary response does not explicitly violate the order-authorization contract, the Worker may use Deno for **market display only**. An unsafe or malformed authorization state from the primary is rejected rather than hidden by fallback. The fallback status is explicitly `DEGRADED_MARKET_ONLY`, with `orders_authorized=false`, no fabricated Emergency Stop state, no fabricated positions, and no paper-ledger values.
- Deno snapshots are labeled `POLLED_FALLBACK`; they are not WebSocket/live-stream data. The Android UI must show a degraded market-fallback state and must not label these snapshots `LIVE`.

### Never fail over these capabilities

- Positions, orders, fills, P&L, risk, audit, funding, opportunities, assessments, candle history, and paper-ledger state remain primary-only. If Render is unavailable, these return unavailable rather than stale or invented data.
- `POST /api/v1/emergency-stop` is forwarded to Render exactly once. It is never retried and never sent to Deno. An unavailable primary produces an explicit error, not a false success.
- The authenticated `/ws/v1/markets` WebSocket is tunneled to Render only, preserving the WebSocket handshake headers. Deno does not pretend to be a WebSocket service. If Render is asleep, Android reconnects and the separate REST fallback may supply a recent display snapshot.
- All other mutation methods and paths are rejected by the Worker.

## 3. Data validation and safety

- Deno uses only `https://demo-fapi.binance.com/fapi/v1/premiumIndex` (public Futures Testnet data).
- Symbols are restricted to the configured allowlist; the default is the existing ten-symbol Solo paper universe.
- Non-finite, malformed, non-allowlisted, future-dated, stale, or incomplete market rows are omitted.
- Market snapshots are accepted only when the source timestamp is within the configured freshness window (default 120 seconds; Worker setting is bounded to 30–300 seconds).
- Every returned market item and top-level response explicitly carries `orders_authorized=false` and `mode=PAPER_ONLY`.
- Deno returns no candles. Chart history, indicators, strategy assessment, opportunity research, and paper simulation remain unavailable when the primary backend is down.
- The Deno fallback is not connected to the paper worker and cannot feed the Risk Guardian or create paper intents.
- The user's bearer token is forwarded only to the Render primary. Cloudflare and Deno use a separate random service-to-service secret: Cloudflare secret `SECONDARY_MARKET_TOKEN` and Deno secret `SOLO_FALLBACK_TOKEN` must match and be at least 32 characters. Deno rejects unauthenticated market requests; `/healthz` remains public. No API token, exchange credential, database URI, or service-role key is stored in source or sent to Deno.

## 4. Provider limits and why this is not a 24/7 guarantee

- Cloudflare Workers Free currently lists 100,000 requests/day and 10 ms CPU per invocation. This Worker is intentionally lightweight and has no scheduled worker loop. [Official limits](https://developers.cloudflare.com/workers/platform/limits/).
- Deno Deploy Free is positioned for personal use and smaller projects. It currently lists 1 million inbound requests/month, 20 GiB egress, 10 active CPU hours/month, and 150 GiB-hours of memory time. Idle apps shut down after roughly 20–30 seconds; the Free plan has no reliability SLA. Recheck plan eligibility if Solo becomes commercial. It is an on-demand fallback, not an always-on process. [Official pricing](https://deno.com/deploy/pricing).
- Render Free web services spin down after 15 minutes without inbound HTTP requests or WebSocket messages, take about a minute to wake, and can restart. A connected foreground WebSocket with keepalives may keep the service active, but that does not cover phone-off/background periods. The Python worker cannot be promised continuous operation on that plan. [Official free-service limits](https://render.com/docs/free).
- Vercel Hobby is intentionally not part of the production architecture: its terms restrict it to personal/non-commercial use, and Hobby cron is limited to once daily. [Official Hobby terms](https://vercel.com/docs/plans/hobby) and [Cron limits](https://vercel.com/docs/cron-jobs/usage-and-pricing). A static GitHub Pages page may be added later for public status/help content, but it cannot run a backend or worker.
- No free tier in this architecture guarantees uninterrupted 24/7 execution, WebSocket continuity, zero downtime, or 100% availability. The design improves graceful degradation and recovery instead.

## 5. Existing infrastructure audit and constraints

Read-only inventory on 2026-11-05 found:

- Render `solo-ai-auto-trader-staging-api`: Free web service, Frankfurt, deploys from `feat/solo-ledger-dashboard-integration`, auto-deploy enabled.
- Render `solo-futures-websocket-staging`: Free web service, Frankfurt, deploys from `fix/futures-context-mainline`, auto-deploy disabled.
- Render `ai-auto-trader-v4-backend`: older Free service whose configured SQLite path is `/tmp/v4data/paper-ledger.sqlite3`; `/tmp` is ephemeral. Do not use it as the authoritative ledger or route the new gateway to it.
- A separate older `ai-auto-trader-backend` service belongs to another repository. It is not part of this cutover.
- Supabase production `solo-ai-auto-trader` and staging `solo-ai-auto-trader-staging` both reported `ACTIVE_HEALTHY` at audit time. Staging has 21 `paper_ledger` tables with RLS enabled and zero security-advisor findings.
- Production security advisor reports informational `rls_enabled_no_policy` findings. Read-only SQL confirmed the private `paper_ledger` schema ACL is `postgres=UC/postgres`; `anon`, `authenticated`, and `service_role` have no schema USAGE. The public `audit_events` table also has no table grants to those API roles. Do not add broad grants or permissive RLS policies just to silence these informational lints. Re-evaluate only if the access model changes.

## 6. Deployment sequence (staging only)

1. Deploy `hosting/deno/main.js` as a Deno Deploy application. Configure `SOLO_MARKET_SYMBOLS` with the existing ten-symbol allowlist and `BINANCE_USDM_TESTNET_REST_ORIGIN=https://demo-fapi.binance.com`.
2. Verify Deno `/healthz` and authenticated-independent `/api/v1/markets` output. Confirm all items have fresh timestamps, `data_mode=POLLED_FALLBACK`, and `orders_authorized=false`.
3. Set the Deno secret `SOLO_FALLBACK_TOKEN` and the Cloudflare secret `SECONDARY_MARKET_TOKEN` to the same newly generated random value (at least 32 characters). Deploy `hosting/cloudflare/wrangler.toml` and `hosting/cloudflare/src/index.js` as a Cloudflare Worker. Set `PRIMARY_API_ORIGIN=https://solo-ai-auto-trader-staging-api.onrender.com`, `SECONDARY_MARKET_ORIGIN` to the Deno deployment origin, and `MAX_MARKET_AGE_SECONDS=120`. Never commit the shared secret.
4. Verify Worker `/healthz`, `/readyz`, primary API reads, WebSocket upgrade forwarding, and simulated primary-outage fallback. Confirm `/api/v1/risk`, `/positions`, `/opportunities`, and `/api/v1/emergency-stop` never fall back to Deno.
5. Only after the above succeeds, set the Android staging build's `PAPER_API_BASE_URL` to the Cloudflare Worker URL. Do not change production builds.
6. Install the staging APK and physically verify: normal Render WebSocket shows LIVE only while connected; simulated full API outage displays `DEGRADED · MARKET FALLBACK`; market-only outage with ledger still reachable displays `CONNECTED · PAPER ONLY · MARKET FALLBACK`; both show a non-LIVE recent snapshot; no false LIVE state; ledger/risk/history remain unavailable; emergency-stop failure is explicit; restoring Render reconnects the WebSocket and restores primary API data.
7. Keep the existing Render API origin available as a rollback target until the staging phone acceptance passes.

## 7. Remaining external gates

The repository implementation cannot create or authenticate the user's Cloudflare/Deno accounts, accept provider terms, or obtain provider-specific deployment URLs. Those are external account actions. Until deployment URLs are available and the staging phone acceptance passes, keep the current Android `PAPER_API_BASE_URL` unchanged and treat the new providers as source-ready, not deployed.
