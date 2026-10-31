# Solo Master Project Roadmap

**Project:** Solo AI Auto Trader  
**Repository:** `lost69680-30/ai-auto-trader-v4`  
**Baseline reviewed:** `main`, 2026-10-31  
**Execution mode:** autonomous analysis + paper-only simulation  
**Safety invariant:** `orders_authorized=False`; no live exchange order placement or cancellation.

> Status discipline: “implemented in source”, “CI-verified”, “deployed”, “runtime-verified”, and “phone-verified” are separate states. A source file or passing unit test does not prove deployment or real-world operation. New roadmap scope is not complete merely because it is documented here.

## Product operating decision

Solo Automation uses **adaptive strategy selection**. Solo continuously analyzes validated market data, selects the best eligible strategy for a specific opportunity, and may choose NO_TRADE / WAIT. User strategy selection is not required for each trade. Manual strategy controls may be added later, but are not required for the current autonomous mode.

## Target architecture

Validated shared market data → Market Watcher → Multi-Timeframe Analysis → Market Assessment / Regime Detection → Adaptive Strategy Selector → independent Risk Manager → paper order validation and simulation → position protection / reconciliation → audit and strategy evaluation.

- The Market Watcher discovers and ranks opportunities; it cannot authorize or execute orders.
- Analysis engines publish evidence and uncertainty, not orders.
- Strategy modules propose; the independent Risk Manager remains the sole risk gate.
- All strategies share one portfolio-wide exposure/risk view.
- No valid opportunity means no trade.
- No live execution, exchange order placement, or order authorization is part of this roadmap.

## Part 0 — Source Integrity, Audit and Safety Baseline

**Status: source audit exists; repeatable release gate remains active work.**

- [x] Maintain a consolidated completeness audit and source manifest.
- [x] Keep Python/backend tests and Android build workflows in GitHub Actions.
- [x] Preserve the hard paper-only invariant and absence of a live order-placement adapter.
- [ ] On every implementation PR, verify root source and `extracted-source/ai-auto-trader-v4-assembly` package parity wherever the deployment packages mirrored files.
- [ ] Verify dependency lock/constraints, migration provenance, artifact provenance and exact commit SHA.
- [ ] Add a release checklist that distinguishes source, CI, staging, production and phone verification.

**Exit gate:** reproducible tests, source parity, no secrets in source/artifacts, and all safety invariants passing.

## Part 1 — Backend Reliability, Worker Lifecycle and Deployment

**Status: partial implementation in source; current production activation and operational verification are not established by this repository inspection.**

- [x] Existing polling worker with bounded retry/backoff and feed-health states.
- [x] Existing PostgreSQL shared-market store with worker lease, heartbeat, durable validated snapshots and idempotent intelligence publication.
- [x] Existing worker lease/stale ownership protections in the shared store.
- [ ] Wire and verify the dedicated Market Watcher worker as an independently supervised runtime process/service.
- [ ] Verify lease acquisition, heartbeat cadence, stale-worker detection, lease expiry and safe takeover.
- [ ] Verify graceful shutdown, worker crash/restart, database outage, network outage and duplicate worker startup.
- [ ] Add/verify durable scheduled-job records, idempotent scan IDs, retry policy and missed-job recovery.
- [ ] Add operational readiness/health reporting, structured metrics and alerts for stale worker, stale market data, repeated errors and missed scans.
- [ ] Keep API and worker independently restartable; API reads only validated, fresh PostgreSQL snapshots.
- [ ] Confirm the actual hosting plan supports continuously running workers, WebSockets and scheduled work before changing hosting or incurring cost.

**Exit gate:** multi-day supervised staging soak; heartbeat and snapshot freshness proven across independent API/worker restarts; no production settings changed without a separate authorization.

## Part 2 — Shared Market Data and Timeframe Coverage

**Status: substantial source components exist; full interval coverage and runtime parity require verification.**

- [x] Public read-only Spot and Futures market-data adapter components exist.
- [x] Existing worker validates closed candles, timestamps, freshness, gaps and feed status.
- [x] Existing PostgreSQL shared market snapshot store exists.
- [x] Existing higher-timeframe aggregation includes complete-year aggregation from validated closed monthly candles.
- [ ] Standardize the required analysis intervals: `1m`, `5m`, `15m`, `30m`, `1h`, `4h`, `1d`, `1w`, `1M` (calendar month), `1Y` (calendar year).
- [ ] Distinguish candle interval from history-range selector in API contracts, Android state and database keys; do not conflate `1m` and `1M`.
- [ ] Verify venue-native monthly interval support and derive annual candles only from a complete, validated set of twelve closed calendar-month candles.
- [ ] Use UTC calendar boundaries for crypto and explicit exchange/session calendars for forex or equities when those feeds are introduced.
- [ ] Mark in-progress candles explicitly; use closed candles for confirmed signals.
- [ ] Persist source, market type, symbol, interval, open/close times, observation time, validation status and provenance.
- [ ] Reject gaps, duplicates, out-of-order timestamps, malformed OHLCV, stale/future data and mixed symbols; never fill missing candles with invented data.
- [ ] Add data retention, backfill, rate-limit budgeting, request-weight accounting and per-venue capability metadata.

**Exit gate:** deterministic interval/aggregation tests and verified fresh source data for every advertised timeframe and market type.

## Part 3 — WebSocket and Realtime Delivery

**Status: ASGI/WebSocket source and CI tests exist; production flag/connection is not yet verified.**

- [x] Existing authenticated, one-way, read-only market WebSocket endpoint exists.
- [x] Existing streaming contract validates snapshots and denies order authorization.
- [ ] Publish worker heartbeat/status, market freshness, scan progress, opportunities, regime, selected strategy, decision rationale and automation health as versioned events.
- [ ] Keep the stream one-way; client messages cannot issue trading, strategy, risk or worker commands.
- [ ] Verify authentication, reconnect, backoff, duplicate/out-of-order event handling and stale-state clearing.
- [ ] Run an authenticated end-to-end staging WebSocket test and restart/recovery drill before claiming deployed WebSocket health.

**Exit gate:** authenticated staging client receives fresh versioned events, detects disconnection/staleness, reconnects safely, and cannot send control commands.

## Part 4 — Android Application (Solo)

**Status: Android source and CI APK build exist; real-device verification remains incomplete.**

- [x] Native Android/Jetpack Compose app source exists and is branded Solo.
- [x] Multi-timeframe chart/analysis source and read-only API connection surface exist.
- [x] CI has built a debug APK in prior verified workflows.
- [ ] Add a Market Watch screen showing active symbols, worker status/heartbeat, last scan, data freshness, market regime, opportunity rank, selected strategy and evidence.
- [ ] Show timeframe analysis summaries and explicit conflicts/insufficient-data states.
- [ ] Display only backend-sourced values with timestamps and freshness; never fabricate prices, status, balances or strategy activity.
- [ ] Show automation mode, WAIT/NO_TRADE reason, paper-only mode and emergency-stop state.
- [ ] Verify navigation, dark/light theme, champagne-gold accent, glass/neumorphic visual system, loading/offline/error states, accessibility and session behavior.
- [ ] Install the CI-built APK on the user's Android phone and complete real-device checks.

**Exit gate:** CI build plus user-device verification against staging API and WebSocket.

## Part 5 — Market Watcher, Multi-Timeframe Analysis and Market Assessment

**Status: partial foundations exist; the unified multi-timeframe backend analysis and watcher-to-selector contracts remain to be implemented and tested.**

### Market Watcher
- [ ] Continuously scan only configured/allowed instruments using the shared validated data layer.
- [ ] Detect trend continuation, pullback, reversal, breakout, failed breakout, momentum shifts and unusual volatility.
- [ ] Screen liquidity, spread, abnormal market conditions, data freshness and venue constraints before ranking opportunities.
- [ ] Persist idempotent scan records and ranked opportunity evidence.
- [ ] Expose watcher heartbeat, last successful scan, next scheduled scan, per-symbol data quality and reasons for exclusions.
- [ ] Notify Android through the read-only API/WebSocket; watcher never authorizes a trade.

### Multi-Timeframe Analysis Engine
- [ ] Analyze all required timeframes independently of which chart the user is viewing.
- [ ] Long-term context: `1Y → 1M → 1W`.
- [ ] Medium-term structure: `1D → 4H → 1H`.
- [ ] Short-term timing: `30M → 15M → 5M → 1M`.
- [ ] Calculate trend, momentum, volatility, market structure, support/resistance zones, volume, candlestick evidence, invalidation and data quality.
- [ ] Distinguish closed from incomplete candles and expose uncertainty/history sufficiency.
- [ ] Detect timeframe conflicts; do not use mechanical majority voting because timeframes are correlated.
- [ ] Treat initial timeframe weights as configurable research hypotheses, not validated probabilities.

### Market Assessment Engine
- [ ] Classify trending, ranging, high-volatility and uncertain regimes.
- [ ] Classify continuation, pullback, reversal, breakout and failed-breakout conditions.
- [ ] Combine multi-timeframe structure, momentum, volatility, liquidity and uncertainty into a traceable assessment.
- [ ] Return structured evidence and explicit NO_TRADE reasons; no order authority.

**Exit gate:** deterministic fixtures for each regime/setup, conflict cases, incomplete data and failure cases; analysis output is reproducible and timestamped.

## Part 6 — Adaptive Strategy Engine and Research Validation

**Status: current heuristic research compares four signal families; the requested autonomous five-style selector is not implemented or validated. Historical evaluation found zero eligible trades in one BTC hourly evaluation; do not claim profitability.**

- [ ] Implement five distinct strategy profiles:
  - Scalping: very short holding periods; `1m/5m`, confirmed by `15m/1h`.
  - Day trading: intraday; `5m/15m/1h`, confirmed by `4h`.
  - Swing: days to weeks; `1h/4h`, confirmed by `1d/1w`.
  - Position: weeks to months; `1d/1w`, confirmed by `1M`.
  - Long-term analysis: months to years; `1w/1M`, confirmed by `1Y`; analysis-only initially.
- [ ] Give each strategy its own eligibility, entry/exit, invalidation, holding-period, cost sensitivity and performance attribution rules.
- [ ] Implement autonomous/adaptive selection among eligible strategies based on market regime, setup evidence, liquidity/spread, realistic execution costs, uncertainty and verified walk-forward/paper performance.
- [ ] Rank opportunities; select the best eligible strategy per opportunity; do not force a trade when none qualifies.
- [ ] Prevent duplicate/conflicting proposals and coordinate correlated exposure across all strategies at portfolio level.
- [ ] Use confidence only when calibrated against out-of-sample results; heuristic scores must be labelled as scores, not probabilities.
- [ ] Compare against no-trade, buy-and-hold and simple baseline strategies.
- [ ] Use chronological walk-forward evaluation and an untouched final holdout; record dataset fingerprints and prevent look-ahead leakage.
- [ ] Include fees, spread, slippage, turnover, drawdown, tail loss, trade distribution and sensitivity analysis.
- [ ] Pause/deprioritize a strategy when evidence, data quality or performance deteriorates; learning cannot alter risk limits or authorization.

**Exit gate:** no-leakage evaluation across multiple symbols and regimes with frozen holdout protocol; no profitability claim without statistically meaningful net-of-cost evidence.

## Part 7 — Independent Risk Manager and Paper Execution

**Status: substantial paper-only controls and simulated ledger exist; full portfolio and runtime integration still require verification.**

- [x] Independent risk guardian and fail-closed paper orchestration exist.
- [x] Paper ledger, simulated fills, emergency stop, stop-loss/take-profit/trailing protection and audit records exist.
- [x] Existing code explicitly denies order authorization.
- [ ] Route every adaptive strategy proposal through one independent Risk Manager.
- [ ] Aggregate exposure across all strategies and symbols; repeated BTC proposals must count as one combined BTC exposure.
- [ ] Validate daily loss, drawdown, allocation, available paper balance, stop/invalidation, size, freshness, emergency stop and venue filters.
- [ ] Failed validation blocks the paper intent and persists a specific audit reason.
- [ ] Verify idempotency, partial simulated fills if modeled, restart recovery, mark reconciliation and protection behavior under stale feeds.
- [ ] Do not let strategy settings, AI, agents, UI or generated configuration weaken independent limits.
- [ ] Keep exchange order placement/cancellation, private account execution and live trading out of scope.

**Exit gate:** adversarial risk tests, portfolio-wide exposure tests, restart/crash recovery and supervised paper soak pass with `orders_authorized=False`.

## Part 8 — Advanced AI Research

**Status: not implemented as an integrated validated runtime.**

- [ ] Only after Parts 0–7 are verified, evaluate PPO/RL, GAT and CTDE offline against transparent deterministic baselines.
- [ ] Require simulator fidelity, point-in-time features, reproducible seeds, versioned models, leakage controls, walk-forward and untouched holdout evaluation.
- [ ] Define typed MCP/A2A messages, authentication, trust boundaries, timeout/retry/idempotency and traceability before any agent orchestration.
- [ ] Agents can research and propose only; no agent can call an order endpoint or bypass Risk Manager.
- [ ] Do not add complexity unless it measurably improves robust out-of-sample results after costs.

**Exit gate:** independently reproducible improvement over baselines without weakening safety.

## Part 9 — Final Release, Operations and Maintenance

**Status: not release-ready.**

- [ ] Complete source, backend, database, API, WebSocket and Android end-to-end verification.
- [ ] Verify backup/restore, schema migration, secret rotation, alerting, incident response, rollback and disaster recovery.
- [ ] Verify long-running uptime, worker restarts, scheduled-job recovery and data freshness.
- [ ] Complete real Android device installation and acceptance testing.
- [ ] Publish versioned release notes, known limitations, operating runbook and safety statement.
- [ ] Keep live trading disabled; any future execution scope requires a separate design, review and explicit authorization.

## Dependency order and implementation sequence

1. Part 0 — baseline, branch isolation, source parity and tests.
2. Part 1 — durable worker lifecycle and operational health.
3. Part 2 — validated shared multi-timeframe data.
4. Part 3 — versioned real-time contracts.
5. Part 5 — watcher, multi-timeframe analysis and market assessment.
6. Part 6 — adaptive strategy selector and evidence-based validation.
7. Part 7 — portfolio-wide risk and paper integration.
8. Part 4 — Android UI consumes stable backend contracts (UI work may proceed in parallel only against agreed contracts).
9. Part 8 — advanced AI research after deterministic baselines.
10. Part 9 — release gates.

## Current safety statement

This roadmap authorizes implementation of analysis, monitoring, research and paper simulation only. It does not authorize live orders, exchange order placement, credential changes, production configuration changes, paid hosting/database resources, or production deployment. All new components must preserve `orders_authorized=False` and fail closed on missing, stale, malformed or contradictory data.
