# Solo Master Project Roadmap

**Updated:** 2026-10-31  
**Project:** Solo AI Auto Trader  
**Execution boundary:** paper-only / read-only. `orders_authorized=False`.

This roadmap preserves the ten-part project structure and records evidence separately from plans. A feature is not considered complete merely because code exists: it must pass relevant tests, staging checks, and phone acceptance where applicable.

## Status rules

- **Implemented in source:** code exists in the referenced project branch.
- **CI verified:** the relevant automated tests/build completed successfully for the exact commit.
- **Staging verified:** the deployed staging service was checked after that commit.
- **Phone verified:** the installed Android build was tested on the user's device.
- **Open:** any missing verification or remaining work keeps the part open.

Never infer live connectivity, profitability, production readiness, or phone installation from a passing unit test.

## Part 0 — Source Integrity, Audit and Project Baseline

**Status: ongoing; historical audit evidence exists, final audit remains open.**

- Preserve the authoritative source tree, dependency manifests, migrations, build workflows and package mirrors.
- Keep root source and the Render assembly package synchronized.
- Verify clean archive extraction, checksums, file manifests, Python compilation and complete test results.
- Maintain a clear inventory of implemented, partial, missing and blocked features.
- Record deployment commit hashes and distinguish source, CI, staging and device verification.
- Re-run the completeness/security audit after each major integration and before release.

## Part 1 — Backend Reliability and Deployment

**Status: substantial source implementation; staging and recovery verification remain ongoing.**

- Supervised backend/API and independent market-data worker lifecycle.
- PostgreSQL-backed shared market snapshots and closed-candle archive.
- Worker ownership leases, heartbeats, stale-worker detection and recovery.
- Durable, idempotent scheduled-job registration, claiming, retry/failure and completion.
- API reads validated shared snapshots rather than maintaining a competing market feed.
- Independent API/worker restart and recovery tests.
- Graceful shutdown, readiness/health reporting, logs and operational alerts.
- Verify backup/restore, deployment/rollback, uptime, worker restart and database recovery in staging.
- Keep Supabase PostgreSQL, GitHub deployment and encrypted server-side secrets. Do not migrate providers or incur charges without separate approval.

## Part 2 — Shared Market Data and Timeframes

**Status: core data path implemented; full interval coverage and source-quality verification remain open.**

Support and distinguish these candle intervals:

- Intraday: `1m`, `5m`, `15m`, `30m`, `1h`, `4h`.
- Higher timeframe: `1d`, `1w`, `1M` (calendar month), `1Y` (calendar year).

Requirements:

- Keep candle interval separate from the historical display-range selector.
- Never confuse `1m` (one minute) with `1M` (one calendar month).
- Use native exchange intervals where available; derive yearly candles only from complete, validated monthly history.
- Use UTC calendar boundaries for crypto. Forex requires a separate broker/feed adapter and correct session/calendar rules before it is enabled.
- Mark in-progress candles and exclude them from confirmed signal evidence.
- Reject stale, duplicated, out-of-order, incomplete or gapped data; never fabricate missing candles.
- Record source, venue, market type, observation time and data freshness.
- Do not treat Testnet liquidity or price behavior as representative of production markets.

## Part 3 — WebSocket and Realtime

**Status: implementation exists; full staging and device recovery validation remains open.**

- Publish validated prices, market context, candle updates, worker health and automation status.
- Reconnect safely after network loss, backend restart and app foreground/background transitions.
- Preserve last-known-good values without presenting them as current when stale.
- Publish Market Watcher scan status, opportunity changes, selected strategy and rationale.
- Test REST/WebSocket consistency, stale-data handling, duplicate events and recovery.

## Part 4 — Android Application

**Status: app and chart foundation exist; new Market Watcher/adaptive-strategy UI and final device acceptance remain open.**

- Preserve Solo branding, dark/light themes and the established navigation.
- Add a Market Watcher view showing worker status, scan freshness, symbols, regime, timeframe context, opportunity ranking, selected strategy and no-trade reasons.
- Show that strategy selection is advisory and that Risk Manager approval is still required.
- Display all supported intervals, including monthly/yearly, with correct labels and data-quality states.
- Preserve authentication/token security and never store exchange private keys in the APK.
- Keep last-known-good data visibly distinct from fresh data.
- Build APK through GitHub Actions, install on the user's Android device and complete physical-device QA.

## Part 5 — Market Watcher, Multi-Timeframe Analysis and Market Assessment

**Status: existing shared Market Watcher worker; new assessment/selection integration is in implementation.**

Keep three distinct responsibilities:

1. **Market Watcher:** continuously monitors configured instruments, checks data quality and freshness, schedules scans, detects changes and publishes results.
2. **Multi-Timeframe Analysis Engine:** evaluates each timeframe independently.
3. **Market Assessment Engine:** interprets the evidence, market regime, direction, structure, uncertainty and conflicts.

Hierarchical timeframe roles:

- Long-term context: `1Y` → `1M` → `1w`.
- Medium-term structure: `1d` → `4h` → `1h`.
- Short-term timing: `30m` → `15m` → `5m` → `1m`.

Assessment inputs include trend, momentum, volatility, ATR, structure, support/resistance, volume, candle patterns, data quality, history coverage and uncertainty.

Detect trending, ranging, high-volatility and uncertain regimes; identify continuation, pullback, reversal, breakout and failed-breakout conditions. Do not use a simple majority vote across correlated timeframes. Higher-timeframe conflicts must remain visible.

## Part 6 — Adaptive Strategies and Evidence Validation

**Status: strategy research exists; new autonomous ranking engine is in implementation. Profitability is unproven.**

Solo Automation uses adaptive strategy selection:

- Scalping.
- Day trading.
- Swing trading.
- Position trading.
- Long-term market analysis (analysis-only initially).

Each strategy has separate required timeframes, holding horizon, entry/exit logic, invalidation, costs and performance attribution.

The adaptive selector must:

- Evaluate only enabled/eligible strategies.
- Rank opportunities using regime fit, aligned timeframe evidence, liquidity, spread, estimated fees/slippage and validated historical performance.
- Select the strongest eligible strategy, or explicitly return **NO_TRADE**.
- Treat long-term analysis as advisory-only.
- Label heuristic fit scores as uncalibrated; never present them as win probabilities.
- Use realistic fees, spread, slippage and funding where relevant.
- Require reproducible walk-forward and untouched holdout testing before claiming strategy quality or profitability.
- Never force a trade because automation is enabled.

## Part 7 — Paper Trading, Portfolio Risk and Position Management

**Status: paper ledger, risk controls and protection foundation exist; further integrated verification remains open.**

- Strategy output is a proposal only and may proceed only to independent Risk Manager review.
- The Risk Manager has exclusive authority to accept or reject paper intents.
- Apply portfolio-wide aggregate exposure across strategies and symbols.
- Revalidate market freshness, sizing, balance, stop/invalidation, daily loss, allocation, leverage and Emergency Stop.
- Validate exchange filters and idempotency before any paper execution simulation.
- Reconcile intents, simulated fills, fees, positions, SL/TP, trailing exits and P&L.
- Failed checks block and record the reason.
- AI, agents, RL, GAT and generated settings cannot override risk policy or authorize orders.
- Keep the live exchange order adapter disabled. `orders_authorized=False` remains mandatory.

## Part 8 — Advanced AI Research

**Status: not started as a validated production capability; research only.**

- Consider PPO/RL, GAT, CTDE and MCP/A2A only after reliable shared data, deterministic baselines and a realistic simulator exist.
- Prevent look-ahead bias, leakage, data contamination and reward hacking.
- Compare against simple baselines using reproducible walk-forward and untouched holdout results.
- Keep all learned outputs advisory; no model may alter risk limits or order authority.

## Part 9 — Final Release, Operations and Maintenance

**Status: open; no final release claim.**

- Complete security, reliability, data-quality, API, WebSocket, Android and risk audits.
- Pass full backend and Android CI for the release commit.
- Complete staging soak, restart/recovery, backup/restore and rollback drills.
- Build a signed release APK and verify installation and behavior on the user's phone.
- Verify no credentials in APK/logs, no unintended live endpoints and no order authorization.
- Publish release notes, known limitations, operational runbook and recovery instructions.
- Keep production release and live trading as separate, explicitly reviewed decisions.

## Dependency order

`Part 0 → Part 1 → Part 2 → Part 3 → Part 5 → Part 6 → Part 7 → Part 9`

Part 4 consumes stable backend contracts and proceeds alongside integration. Part 8 remains later research work. Do not skip data/reliability gates to accelerate strategy or AI work.

## Current implementation branch

The new market-assessment/adaptive-strategy work is being developed in a separate feature branch and draft PR, based on the shared-market-data/reliability branch. It is not merged or deployed. Automated test results must be checked against the latest commit before this roadmap status is advanced.

## Explicit limitations

- The shared worker currently uses Binance USDⓈ-M Futures market data in its Testnet configuration; Testnet observations are not production-market evidence.
- A Forex data/broker adapter and Forex session-calendar implementation are not established by this work.
- Strategy fit scores are heuristic, not calibrated confidence.
- No profitability guarantee is supported.
- No live exchange order authorization is part of this roadmap update.
