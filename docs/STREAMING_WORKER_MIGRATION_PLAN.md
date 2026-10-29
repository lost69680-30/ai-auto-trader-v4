# Solo Streaming API and Analysis Worker — Staging Plan

## Scope and safety boundary

This plan targets a separate staging implementation in `feature/streaming-worker-staging`. It does not change the Render service configuration, production deployment, Supabase schema/data, credentials, or billing. The exchange execution boundary remains paper-only: every public response and analysis record must preserve `orders_authorized: false`; no exchange order-placement or cancellation adapter is in scope.

## Verified starting point

- The current deployed command starts `app.engines.read_only_api`.
- That module runs a background public REST market poller and serves an authenticated WSGI API.
- Its status payload explicitly reports `research_pipeline: NOT_STARTED` and `paper_simulation: NOT_STARTED`.
- The repository contains a separate paper-service composition layer, but the deployment guide identifies operational gaps: persistent state must be verified, active signal watches are process-local, and target-host market connectivity/restart recovery need explicit testing.
- The existing API supports REST, not WebSocket upgrades. Changing hosting alone cannot add WebSocket support.

## Proposed implementation sequence

### Phase 1 — Contracts and test harness (this branch)

1. Specify a streaming message contract for validated market snapshots and analysis status. Include schema/version, symbol, observation time, freshness, health, and `orders_authorized: false`.
2. Add deterministic tests for authentication, malformed frames, stale data, duplicate/out-of-order timestamps, disconnect/reconnect, slow consumers, and emergency-stop visibility.
3. Keep the existing REST API contract unchanged. Do not advertise streaming as available until the endpoint and runtime are actually implemented and tested.

### Phase 2 — Streaming transport

1. Implement a WebSocket endpoint in a server/runtime that explicitly supports WebSocket upgrades. Do not try to bolt WebSockets onto Python's `wsgiref.simple_server`.
2. Require the existing strong backend API token via a documented secure authentication mechanism; never put exchange credentials in messages or the Android APK.
3. Stream only validated, allowlisted public market data and read-only paper-state updates.
4. Apply bounded message sizes, per-client send queues, backpressure/disconnect policy, heartbeat and idle timeouts, and bounded reconnect/backoff.
5. Preserve the existing REST endpoints as fallback; clients must show stale/offline status and must never invent prices.

### Phase 3 — Supervised analysis worker

1. Run the paper analysis/simulation engine as an explicitly supervised worker, separate from the HTTP process where practical.
2. Prevent duplicate worker instances and duplicate scheduled runs with a durable lease/idempotency key or equivalent guard.
3. Read only fresh, closed, validated candles. Block new simulated entries on stale data, uncertain ledger state, active emergency stop, missing risk baseline, or failed risk checks.
4. Persist paper ledger state to the already configured PostgreSQL/Supabase connection when verified; never silently fall back to ephemeral `/tmp` SQLite when PostgreSQL is configured or unavailable.
5. Make worker health observable: last successful poll, last analysis time, current lag, last error class, stale symbols, and worker heartbeat. Do not log secrets or full DSNs.

### Phase 4 — Staging-only deployment and acceptance

Use an isolated staging service/environment. Do not point it at production mutation paths; use a separately scoped staging database/schema or a read-only verification path approved by the owner. No production cutover is part of this phase.

## Required automated tests

- REST compatibility: existing status, markets, candles, risk, orders, positions, and audit contracts continue to pass.
- Authentication: missing/invalid token rejected; secrets never appear in errors/log payloads.
- WebSocket authentication: unauthenticated clients rejected before receiving data.
- Data quality: malformed, stale, future-dated, duplicate, gapped, and out-of-order market frames are not broadcast as healthy.
- Safety invariant: every emitted snapshot/analysis response has `orders_authorized: false`; no order execution route is introduced.
- Resilience: upstream outage, reconnect storms, slow consumers, cancellation, and worker exceptions do not kill the API or bypass stale-data blocking.
- Idempotency: duplicate worker starts or repeated scheduled triggers do not duplicate ledger intents.
- Persistence/recovery: emergency-stop and paper positions survive process restart; startup fails closed on unavailable/invalid configured PostgreSQL.
- Performance: bounded memory under a slow WebSocket client and multiple clients; rate limits/backoff avoid upstream request storms.
- CI: focused tests, then `python -m pytest -q tests backend/tests` and `python -m compileall -q app backend`.

## Operational acceptance criteria

- Staging health checks prove API and worker are independently alive.
- WebSocket receives a validated update, detects stale data, reconnects after disconnect, and falls back to REST without fabricating values.
- A deliberate upstream outage marks the feed stale/offline and blocks new paper entries.
- A restart restores durable ledger/emergency-stop state and does not restore untrusted process-local signal watches.
- Supabase connectivity and persistence are verified without printing connection strings or weakening RLS.
- Resource usage is measured against the hosting plan before any migration decision.
- A manual approval is required before changing Render production settings, moving production traffic, changing Supabase schema/data, or creating paid resources.

## Explicit non-goals

- No real-money/live trading, exchange order placement, leverage, or private-account execution.
- No changes to current Render service settings or deployments.
- No Supabase migration or permission changes.
- No credential rotation or secret display.
- No claim of profitability from unit tests, synthetic replay, or paper fills.

## Phase 3 entry-gate audit (2026-10-29)

Repository inspection found blockers that must be resolved before starting the analysis worker in a deployed environment:

- `PaperServiceApplication.from_env()` constructs `BinanceUsdMFuturesAdapter`, which targets the USD-M futures API. The user's current target is crypto spot read-only/testnet analysis; wiring this service in unchanged would analyze the wrong market domain.
- `PaperServiceApplication.create()` opens `SQLitePaperLedger` directly. It does not use the API's `_open_ledger()` PostgreSQL selection path or `PostgresPaperLedger`. Pointing both processes at a SQLite path would not provide shared durable state on an ephemeral host; pointing at Supabase requires an explicit compatible ledger composition and a verified schema.
- The API's existing market cache and the paper-service worker are separate data paths. The worker must consume the same validated spot candle/price contract or use a separately tested spot adapter, with explicit freshness and symbol allowlists.
- A process-local loop alone is not duplicate-run protection. Before any deployed worker can write paper intents, it needs a durable lease/idempotency guard and restart/reconciliation tests against the selected database.
- The existing streaming endpoint currently emits validated market snapshots only. It does not claim to stream analysis results, worker health, positions, or emergency-stop updates.

### Required gate before worker implementation/deployment

1. Define the spot market adapter interface and prove it with deterministic contract tests; keep all exchange calls public/read-only.
2. Refactor paper-service ledger injection so PostgreSQL can be selected explicitly without SQLite fallback, and test connection/schema failure as a startup blocker. Do not apply a migration or alter Supabase permissions as part of this work.
3. Add a durable worker lease/idempotency mechanism, plus tests for concurrent starts, retry after crash, stale data, emergency stop, and restart recovery.
4. Add an explicit non-production configuration validation that refuses to start if market mode, database backend, simulated equity/risk settings, or symbol allowlist are missing or inconsistent.
5. Only then run isolated tests. Staging deployment remains a separate approval gate; no Render settings, production traffic, Supabase schema/data, secrets, or billing are changed by this audit.

**Current decision:** worker deployment is blocked pending these gates. Existing code is useful research/paper-simulation infrastructure, but it is not yet a safe drop-in worker for the current spot/read-only API. All worker outputs must continue to carry `orders_authorized: false`; no exchange order-placement path is permitted.
