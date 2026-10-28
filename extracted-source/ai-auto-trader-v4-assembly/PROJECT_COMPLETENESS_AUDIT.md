# AI Auto Trader v4 — Consolidated Project Audit and Remaining-Work Register

**Audit updated:** 2026-10-17  
**Scope:** Python research/paper backend, Android frontend source, packaging, automated tests, runtime safety, and deployment readiness.  
**Execution policy:** Paper-only. `orders_authorized=False` is a hard invariant. No exchange order-placement/cancellation adapter, private account access, or live execution is included.

## Executive summary

The repository is a substantial research-to-paper-trading prototype, not yet a production trading service. The Python backend has a public market-data adapter, feed worker, scanner/research and liquidity-analysis modules, price planning, signal validation/watching, independent risk checks, SQLite paper ledger, simulated fills/protection, a local service composition, connectivity preflight, backtesting utilities, and synthetic replay/soak tests. The Android app is a native Jetpack Compose frontend shell with dashboard, markets/charts, agent network, positions, and risk screens. It is not yet connected to an authenticated backend and is not a verified APK.

**This milestone resumed backend work and fixed a restart-safety gap:** because active signal watches and cached price plans are process-local, the service now durably cancels pending paper intents on startup rather than allowing stale intents to be filled after restart. Cancellation is recorded in SQLite audit events. The service requires fresh data and a newly validated signal for a new intent. No exchange execution path was added.

## Current verification snapshot

- Python tests: **257 passed** (`python -m pytest -q`).
- Python compilation: `python -m compileall -q app backend` passed.
- Deterministic synthetic 24-hour paper soak: passed; 1,473 replay events, 288 recorded intents/fills/protection exits, 24 simulated restart events, nine stale-update rejections, zero open positions at completion, and `orders_authorized=False`.
- Restart/concurrency tests: pending intents are cancelled durably at service startup; open positions and emergency-stop state survive reopen; a recovered stop blocks entries; stale/future marks are unhealthy; duplicate intent submission is idempotent across 32 simultaneous submissions using independent SQLite connections; timestamp reversal is rejected. This is stronger restart coverage but not exhaustive process-kill injection between every database/protection transition.
- Funding ledger milestone: durable `paper_funding_events` table, unique event IDs, sign-correct long/short cash flows, event-time bounds, duplicate/conflict detection, audit events, and realized/unrealized net P&L inclusion; covered by focused tests.
- Account-risk milestone: added UTC-day realized P&L and realized-equity drawdown from durable close/funding events, plus durable mark-to-market snapshots that combine ledger-derived realized equity with open-position unrealized P&L net of opening fees. Snapshot high-water equity/drawdown persists across restarts; timestamps cannot move backward, and same-time retries must be idempotent. Durable-ledger orchestration fails closed if the baseline/report is missing, open-position marks are stale/future-dated, or configured mark-to-market drawdown is reached. Caller-supplied `daily_pnl` and open-position counts are not trusted on the durable-ledger path.
- External exchange connectivity: **failed from this environment** on 2026-10-17. The public Testnet server-time request could not be reached (`MarketDataError: unable to retrieve public market data`); later probes were skipped. This is not a target-host test. Run it from the actual target host before proceeding.
- Android build: **not verified**. Java is available here, but Gradle, Android SDK and ADB are unavailable, so no local APK build or device installation could be performed. GitHub Actions must succeed before APK status can be called verified.
- Repository publication: a successful push of the latest assembled source has not been confirmed.

The synthetic soak is a deterministic reliability test, not live-market validation or evidence of profitability. The scripted P&L must not be used as a performance claim.

## Feature-by-feature status

| Feature | Status | Remaining gap |
|---|---|---|
| Public USDⓈ-M Futures market data | Implemented in source; external connectivity unverified | Run connectivity preflight from target host; measure rate limits, latency, gaps and freshness |
| Symbol allowlist and contract metadata checks | Parsed tick size, quantity step, min/max quantity and min notional; standalone fail-closed quantity validator added | Wire filter validation into the runtime sizing-to-intent path; verify venue filter applicability and precision edge cases |
| Scanner, opportunity and strategy research | Implemented as heuristic research pipeline | Calibrate probabilities; compare against simple baselines; evaluate with leakage-safe walk-forward and holdout tests |
| Liquidity/market structure and boundary-excursion research | Source modules and tests included | Verify each module is wired into candidate scoring as intended; add independent out-of-sample validation |
| ATR price planning and signal validation | Implemented | Validate edge cases under gaps, volatility jumps, rounding and stale data |
| Signal watcher and handoff | Implemented in-process | Watch state is not restored after process restart; stale pending intents are now cancelled on startup |
| Independent risk guardian | Implemented with malformed/non-finite input rejection, stop-distance recomputation, and ledger-derived daily realized-loss input for durable-ledger orchestration | Add property-based/adversarial tests, verify limits cannot be weakened by strategy configuration, and add a separate ledger-derived unrealized exposure/drawdown control |
| SQLite paper ledger and simulated fills | Implemented; durable local file state; realized-risk report added | Backups/restore drills, schema migration policy, multi-process locking review, independent unrealized-equity snapshots and account-level P&L reconciliation |
| Stop-loss/take-profit/trailing protection | Simulated in paper mode | Test crash windows and protection behavior against missing/stale data; no real exchange-side protective orders exist |
| Emergency stop | Persisted paper stop; cancels pending paper intents | No authenticated remote control plane; Android button remains disabled while backend is disconnected |
| Restart behavior | Improved this milestone: pending intents cancelled on service startup | Open positions persist, but operational restart drill must prove protection resumes only with fresh marks and correct timestamps |
| Futures funding cash flows | Durable paper ledger plus public funding-rate history fetch and duplicate/timestamp validation | Public rates are not account-specific cash payments; authenticated account income history and actual payment reconciliation remain unavailable/unimplemented |
| Futures account simulation | Partial | Funding, contract multipliers, leverage/margin modes, maintenance margin/liquidation, position netting, partial fills, queue priority and market impact are not comprehensively modeled |
| Cost model | Configured fees/slippage; approximate | Calibrate maker/taker assumptions, spreads, market impact, funding and adverse-selection costs; report sensitivity ranges |
| Backtesting | Utilities/source included | Establish leakage-safe data splits, survivorship/selection controls, realistic costs, benchmark strategies, reproducible artifacts and holdout gates |
| PPO/RL, GAT, CTDE | Not implemented/validated as a running integrated system | Treat as research phase only after simulator fidelity, provenance, baseline and holdout evidence; no policy may bypass risk gate |
| MCP/A2A agent communication | Not implemented/validated as live orchestration | Define typed versioned messages, auth, timeouts, retries, idempotency, tracing and trust boundaries before connecting agents |
| Backend API/authentication | Source implementation added: bearer-token authenticated GET-only WSGI API for status, positions, orders, risk, audit and funding; market endpoint explicitly says unavailable until worker wiring | Not deployed; add rate limiting, operational monitoring and TLS reverse proxy; market worker is not connected to API |
| Multi-user hosting and persistence | Missing/unverified | Choose deployment target, managed database, secret management, backups, health/readiness, alerting and rollback procedures |
| Android dashboard | UI source includes optional HTTPS read-only API connection card; token is session-memory only; status and positions endpoint are checked and status/open-position/feed/emergency-stop fields are displayed | Android build and API integration remain unverified; extend risk/position rendering after CI build and target-host API checks |
| Android dark/light theme and agent network | UI source milestone | Verify behavior/build on device and ensure all screens honor the selected theme consistently |
| APK | Not verified | Build via GitHub Actions, resolve CI errors, install on Android, and test navigation, theme and network/error states |
| GitHub publication/CI | Backend test workflow and Android APK workflow included; APK workflow now runs on matching pull requests and pushes and verifies artifact exists | Confirm exact branch/commit, obtain successful CI run/artifact, and install-test APK on device |
| Production/live trading | Deliberately not implemented | Requires a separate authorization and safety review; not part of the current scope |

## Critical safety invariants

1. Missing, stale, malformed, contradictory or unverified market data blocks candidate creation and simulated fills.
2. Risk validation is independent of strategy, GAT/RL policy, agents and UI; any risk-service error must fail closed.
3. Agent tools and strategies must never have direct access to an exchange order-placement endpoint.
4. No strategy-controlled configuration may increase independent risk limits.
5. The service remains paper-only; all reports must keep `orders_authorized=False`.
6. On restart, process-local trigger and price-plan state is not trusted. Pending intents are cancelled durably; a new intent requires fresh data and fresh validation.
7. Do not describe synthetic results as profitability evidence or describe disconnected agent cards as live agents.

## Remaining work register and recommended order

### P0 — Keep paper execution safe and make runtime behavior reliable

- [x] Cancel pending paper intents at service startup because trigger/watch state is not restored; persist cancellation reason and audit events.
- [x] Add restart checks for pending intent cancellation, open-position persistence, emergency-stop persistence, stale/future mark rejection, and concurrent duplicate intent idempotency. [ ] Still add process-crash injection between every ledger/protection transition and test stale first market frames through the complete service pipeline.
- [x] Define feed-outage policy: block new entries; preserve existing positions and last stored mark; label mark/P&L stale rather than presenting it as current; do not automatically close based on stale prices; resume mark-dependent protection only after a fresh, valid timestamped mark. This policy is covered at the ledger risk-report level; full service/UI behavior still needs integration tests.
- [x] Add account-level risk reporting that derives daily realized loss and peak-to-trough realized-equity drawdown from ledger history; durable-ledger orchestration uses the derived daily P&L and actual open-position count and fails closed without an initial-equity baseline.
- [x] Add durable mark-to-market equity snapshots and a fail-closed gate for stale/future marks and configured mark-to-market drawdown; add restart persistence, timestamp rollback, and idempotency/conflict tests. Remaining: inject process crashes at each state transition, test stale-first-frame behavior end-to-end, and complete multi-process/backup/migration drills.
- [x] Verify same-process concurrent duplicate intent submission and idempotency. [ ] Still verify multi-process SQLite locking, backup/restore and schema migration behavior.

### P1 — Validate real public market data and model the venue more realistically

- [ ] Run `python -m backend.market_connectivity_check --symbol BTCUSDT` from the actual cloud/runtime environment. Do not proceed if server time, mark and closed-candle checks fail.
- [ ] Run a supervised multi-day fresh-market-data paper soak only after connectivity succeeds. Preserve logs and DB; monitor uptime, retries, HTTP 429/418, stale frames, candle gaps, rejects, fills, exits, costs and drawdown.
- [x] Parse tick size, step size, min/max quantity and minimum notional where present; add a fail-closed paper quantity validator with Decimal arithmetic. [ ] Wire validator into all sizing paths; contract multipliers and full venue-specific filter matrix remain incomplete.
- [x] Implement a basic funding cash-flow ledger: persist each supplied funding event with timestamp/rate/notional, sign-correct long/short payment, idempotent event IDs, conflict detection, audit event, and inclusion in net P&L.
- [x] Add a public funding-rate history reader with timestamp/rate validation for schedule cross-checks. [ ] Add missing/late-event reconciliation and account-specific actual-payment reconciliation; public rate history alone is not sufficient.
- [ ] Add futures margin, leverage, maintenance-margin/liquidation and position-netting models, or clearly constrain the simulator to unleveraged linear exposure until those models are tested.
- [ ] Improve fees/slippage/spread/impact assumptions and publish sensitivity results rather than a single optimistic estimate.

### P1 — Backend operations and Android connection

- [x] Add a token-authenticated, GET-only read-only API for status, positions, orders, risk snapshots, audit events and funding records; no mutation/execution routes. The markets endpoint reports UNAVAILABLE until worker state is wired in.
- [x] Add bearer-token authentication, minimum token length, method/path allowlisting, no-store headers and safe error responses. [ ] Add rate limiting, structured operational logs and deployment-grade health/readiness; expose only behind HTTPS reverse proxy.
- [ ] Deploy to a selected host with durable storage, secrets handled outside source, backups, restore test, monitoring, alerting and rollback plan.
- [x] Add an Android read-only API connection card that accepts the token only in memory, requires an HTTPS base URL, and distinguishes connected, unauthorized and connection-error states. [ ] Build-test and expand rendering of positions/market/risk API data.
- [ ] Build APK in GitHub Actions, install on a real Android device, and validate all screens, navigation, theme persistence, API failures and accessibility.

### P2 — Evidence-based strategy and advanced AI

- [ ] Make backtests reproducible with fixed data versions, fees/funding/slippage, walk-forward splits and a final untouched holdout.
- [ ] Compare against buy-and-hold, no-trade, and simple indicator baselines; report net returns, volatility, max drawdown, tail loss, turnover, win/loss distribution and confidence intervals.
- [ ] Add feature provenance and strict point-in-time data handling to prevent look-ahead leakage.
- [ ] Only after baselines pass, prototype PPO/RL and GAT offline with CTDE if useful; record model versions, seeds, training data, reward design and evaluation artifacts.
- [ ] Define versioned agent messages and MCP/A2A boundaries with timeouts, retries, idempotency, tracing and adversarial-input tests. Agents can propose; the independent risk guardian alone decides whether a paper intent may be recorded.

### P3 — Separately reviewed exchange testnet execution (not approved in this milestone)

- [ ] Do not implement or enable until P0/P1 controls and realistic simulation are verified.
- [ ] If separately authorized later, create a dedicated testnet-only adapter isolated from strategy/agents, with client order IDs, duplicate-submit protection, partial fill/cancel-replace lifecycle, exchange reconciliation, exchange-side protection, crash recovery, and persistent kill switch.
- [ ] Keep production/live execution separately disabled and require a separate explicit review and authorization.

## Verification commands

From the project root, with Python 3.11+ and test dependencies installed:

```bash
python -m pytest -q
python -m compileall -q app backend
python -m app.engines.paper_soak --hours 24
python -m backend.market_connectivity_check --symbol BTCUSDT
```

The connectivity command uses public read-only endpoints only. A successful unit test is not proof of external connectivity. Run the connectivity preflight in the exact environment intended for the service.

## Final conclusion

The backend now has 257 passing tests, stronger restart/concurrency coverage, explicit feed-outage policy, durable mark-to-market risk reports, a fail-closed quantity-filter validator, and public funding-rate history inspection. A token-authenticated read-only API and optional Android API connection UI are implemented in source, but are not deployed/build-verified. Public Testnet connectivity failed in this execution environment, so no fresh-market soak is justified yet. Actual account funding-payment reconciliation, per-symbol fee/slippage wiring, full crash injection, backup/restore/migration drills, target-host connectivity, API deployment and APK/CI verification remain open. The system remains paper-only; advanced AI and exchange execution must remain blocked until evidence gates pass.
