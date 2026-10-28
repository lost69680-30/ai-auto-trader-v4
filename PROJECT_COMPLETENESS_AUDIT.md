# AI Auto Trader v4 — Consolidated Project Audit and Remaining-Work Register

**Audit updated:** 2026-10-28  
**Scope:** Python research/paper backend, Android frontend source, packaging, automated tests, runtime safety, and deployment readiness.  
**Execution policy:** Paper-only. `orders_authorized=False` is a hard invariant. No exchange order-placement/cancellation adapter, private account access, or live execution is included.

## Executive summary

The repository is a substantial research-to-paper-trading prototype, not a production trading service. The Python backend includes public market-data adapters, strategy/liquidity research, price planning, independent risk checks, a durable SQLite paper ledger, simulated fills/protection, backtesting and synthetic replay/soak tests. The native Jetpack Compose Android app is branded Solo and includes markets/charts, multi-timeframe analysis and a read-only API connection surface. GitHub Actions has successfully built a debug APK and run the Android unit tests, but installation and hands-on checks on the user's phone are still pending. The authenticated read-only backend is deployed on Render. Its current process uses `/tmp/v4data/paper-ledger.sqlite3`, so persistence across redeploys/restarts is not yet deployment-grade.

**Recent safety milestones:** the Android analysis now rejects malformed, stale, unclosed, gapped, duplicate and out-of-order candle histories instead of treating them as trustworthy; relative-volume analysis excludes the current candle from its comparison baseline. A second independent backend runtime check blocks gapped candle sequences even when a frame claims to be healthy. End-to-end tests block a stale first market poll, and crash-injection tests verify stop-loss/take-profit recovery after a mark is persisted but before a close is recorded. Backtests now fail closed when supplied candle timestamps do not follow the configured interval. These changes remain paper-only and do not add live exchange order placement.

## Current verification snapshot

- Python tests: **289 passed** in the latest verified candidate-branch run (`python -m pytest -q tests backend/tests`); the cadence-validation change is undergoing the final package-parity CI run before merge.
- Python compilation: `python -m compileall -q app backend` passed.
- Deterministic synthetic 24-hour paper soak: passed in the existing test suite; 1,473 replay events, 288 recorded intents/fills/protection exits, 24 simulated restart events, nine stale-update rejections, zero open positions at completion, and `orders_authorized=False`. This is synthetic reliability evidence, not live-market performance.
- Restart/concurrency tests: pending intents are cancelled durably at service startup; open positions and emergency-stop state survive reopen; a recovered stop blocks entries; stale/future marks are unhealthy; duplicate intent submission is idempotent across simultaneous submissions and independent SQLite connections; timestamp reversal is rejected. Added crash-injection tests confirm stop-loss/take-profit recovery after the threshold-crossing mark commits and an end-to-end test confirms a stale first feed frame does not enter analysis. Exhaustive crash injection, backup/restore, schema-migration and operational restart drills remain open.
- Funding ledger milestone: durable `paper_funding_events` table, unique event IDs, sign-correct long/short cash flows, event-time bounds, duplicate/conflict detection, audit events, and realized/unrealized net P&L inclusion; covered by focused tests.
- Account-risk milestone: added UTC-day realized P&L and realized-equity drawdown from durable close/funding events, plus durable mark-to-market snapshots that combine ledger-derived realized equity with open-position unrealized P&L net of opening fees. Snapshot high-water equity/drawdown persists across restarts; timestamps cannot move backward, and same-time retries must be idempotent. Durable-ledger orchestration fails closed if the baseline/report is missing, open-position marks are stale/future-dated, or configured mark-to-market drawdown is reached. Caller-supplied `daily_pnl` and open-position counts are not trusted on the durable-ledger path.
- External connectivity: an earlier public Testnet probe failed from the execution environment on 2026-10-17. Current Render deployment is live, and logs from 2026-10-28 reported `market feed status=HEALTHY`, two healthy symbols and `orders_authorized=False`. That log is not proof of a Testnet order/execution connection. No multi-day, supervised paper soak or successful explicit target-host Testnet preflight has been documented.
- Android build: **CI-verified**. GitHub Actions passed Python tests/compilation, Android market-analysis unit tests, and the Gradle debug APK build/upload. The newest successful candidate-branch run produced a roughly 16 MB APK; installation, weekly/monthly/yearly timeframe behavior, authentication, theme, navigation and API failure states still need real-device checks.
- Repository publication: source changes are committed to GitHub and prior safety improvements are merged to `main`. The current backtest cadence fix is on a candidate branch; its final Render-copy parity workflow must pass before merge.

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
| Backtesting | Deterministic research simulator; now rejects gaps/interval mismatches instead of simulating them as regular bars | Establish leakage-safe data splits, survivorship/selection controls, realistic venue costs, benchmark strategies, reproducible artifacts and untouched holdout gates; simulator still omits stops, liquidation, depth/impact, and funding-payment sign |
| PPO/RL, GAT, CTDE | Not implemented/validated as a running integrated system | Treat as research phase only after simulator fidelity, provenance, baseline and holdout evidence; no policy may bypass risk gate |
| MCP/A2A agent communication | Not implemented/validated as live orchestration | Define typed versioned messages, auth, timeouts, retries, idempotency, tracing and trust boundaries before connecting agents |
| Backend API/authentication | Bearer-token-authenticated GET-only API deployed on Render; unauthenticated health probe returns 401 as expected; live logs report `orders_authorized=False` | Add rate limiting, formal health/readiness and operational monitoring; confirm full API/dashboard integration on-device; maintain no mutation/order route |
| Multi-user hosting and persistence | Missing/unverified | Choose deployment target, managed database, secret management, backups, health/readiness, alerting and rollback procedures |
| Android dashboard | UI source includes optional HTTPS read-only API connection card; token is session-memory only; status and positions endpoint are checked and status/open-position/feed/emergency-stop fields are displayed | Android build and API integration remain unverified; extend risk/position rendering after CI build and target-host API checks |
| Android dark/light theme and agent network | UI source milestone | Verify behavior/build on device and ensure all screens honor the selected theme consistently |
| APK | Debug APK built and uploaded successfully by GitHub Actions | Install on the real Android phone and verify navigation, 1W/1M/1Y history coverage, chart/timeframe changes, theme, API authentication/errors and accessibility |
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
- [x] Add restart checks for pending-intent cancellation, open-position/emergency-stop persistence, stale/future mark rejection, concurrent duplicate intent idempotency, stop-loss/take-profit recovery after a simulated crash at the persisted-mark boundary, and stale-first-frame end-to-end blocking. [ ] Still inject crashes at other ledger transitions and run operational restart drills.
- [x] Define feed-outage policy: block new entries; preserve existing positions and last stored mark; label mark/P&L stale rather than presenting it as current; do not automatically close based on stale prices; resume mark-dependent protection only after a fresh, valid timestamped mark. This policy is covered at the ledger risk-report level; full service/UI behavior still needs integration tests.
- [x] Add account-level risk reporting that derives daily realized loss and peak-to-trough realized-equity drawdown from ledger history; durable-ledger orchestration uses the derived daily P&L and actual open-position count and fails closed without an initial-equity baseline.
- [x] Add durable mark-to-market equity snapshots and a fail-closed gate for stale/future marks and configured mark-to-market drawdown; add restart persistence, timestamp rollback, and idempotency/conflict tests. [x] A crash-injection test covers one persisted-threshold-mark / delayed-close boundary and a service test covers stale first-frame behavior. [ ] Other transition crash points, backup/restore, schema migrations and production-host persistence remain unverified.
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

- [x] Deploy a token-authenticated, GET-only read-only API for status, positions, orders, risk snapshots, audit events and funding records; no mutation/execution routes. [ ] Verify all returned market data is wired and displayed in the Android app and independently validate endpoint freshness.
- [x] Add bearer-token authentication, minimum token length, method/path allowlisting, no-store headers and safe error responses. [ ] Add rate limiting, structured operational logs and deployment-grade health/readiness; expose only behind HTTPS reverse proxy.
- [x] Deploy the authenticated read-only API to Render and verify the service reports live. [ ] Replace the current `/tmp` SQLite path with durable storage before any multi-day soak that depends on persistent state; complete backups, restore test, monitoring, alerting and rollback plan.
- [x] Add an Android read-only API connection card that accepts the token only in memory, requires an HTTPS base URL, and distinguishes connected, unauthorized and connection-error states. [ ] Build-test and expand rendering of positions/market/risk API data.
- [ ] Build APK in GitHub Actions, install on a real Android device, and validate all screens, navigation, theme persistence, API failures and accessibility.

### P2 — Evidence-based strategy and advanced AI

- [x] Validate backtest candle cadence against `candle_interval_minutes` and fail closed on missing/irregular bars. [ ] Add fixed dataset fingerprints, walk-forward splits, a final untouched holdout and realistic/verified per-symbol costs.
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

The latest candidate branch passed 289 Python/backend tests, Python compilation, Android market-analysis unit tests and an APK build/upload. The source now rejects gapped/stale candle histories at both Android analysis and the paper-runtime boundary; backtest cadence enforcement is being verified with an exact Render-package source comparison before merge. The authenticated GET-only API is deployed on Render, but its SQLite file currently resides under `/tmp`, so durable restart/deploy persistence is not established. Render logs on 2026-10-28 showed two healthy market-feed symbols and `orders_authorized=False`; this is not an execution-Testnet verification or a multi-day paper soak. Actual phone installation, durable storage/backups, explicit target-host Testnet preflight, realistic cost/funding reconciliation, walk-forward/holdout evaluation, and other crash/migration drills remain open. The system remains paper-only; RL/GAT/agent execution and live exchange execution must remain blocked until evidence gates pass.
