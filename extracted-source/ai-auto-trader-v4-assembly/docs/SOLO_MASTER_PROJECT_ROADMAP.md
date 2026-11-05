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
- Run scheduled scans on an independent job-runner thread so long scans do not block market-feed polling or worker heartbeats.
- API reads validated shared snapshots rather than maintaining a competing market feed.
- Independent API/worker restart and recovery tests.
- Graceful shutdown, readiness/health reporting, logs and operational alerts.
- Verify backup/restore, deployment/rollback, uptime, worker restart and database recovery in staging.
- **Hosting gate:** the current `solo-futures-websocket-staging` Render service is on the Free plan. Render's current docs say Free web services spin down after 15 minutes without inbound HTTP requests or WebSocket messages; this service's worker/API processes received SIGTERM at 2026-10-31 15:41 UTC and its Supabase worker lease became stale. It is not suitable as the always-on target without a plan/host change.
- **Zero-spend provider research (2026-10-31):** Northflank Sandbox currently advertises a free $20/month resource allowance, two services, two jobs, one addon and always-on/no-sleep workloads. However, Northflank's billing docs also say a payment method is required and new accounts default to a $50 billing threshold; the Sandbox page and billing docs are not sufficiently clear about a hard overage stop. It is a staging candidate only, not approved for setup or production. Railway Free currently advertises a 30-day $5 trial followed by $1/month; Hobby starts at $5/month and usage above included credit is billed. Oracle Cloud Always Free advertises up to 2 OCPU and 12 GB RAM for eligible Ampere instances, but capacity and self-managed operations are constraints. None is selected.
- Preserve the zero-spend constraint. No provider migration, paid plan, payment method, or new service is authorized. Keep Supabase PostgreSQL, GitHub deployment and encrypted server-side secrets.
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

The Continuous Market Search Engine extends this shared watcher; it must reuse the shared data feed and publish durable candidate evidence rather than create a competing feed. Its scan cadence, instrument coverage and data-quality failures must be observable.

## Part 6 — Adaptive Strategies and Evidence Validation

**Status: strategy research exists; new autonomous ranking engine is in implementation. Profitability is unproven.**

Solo Automation uses adaptive strategy selection:

- Scalping.
- Day trading.
- Swing trading.
- Position trading.
- Long-term market analysis (analysis-only initially).

Each strategy has separate required timeframes, holding horizon, entry/exit logic, invalidation, costs and performance attribution.

The Multi-Strategy Assessment Engine must preserve separate assessments and decisions for each strategy. A rejection by one strategy does not cancel another strategy's independent opportunity. Strategy-specific trigger, invalidation, evidence and expiry requirements must be retained. Long-term analysis remains analysis-only and cannot enter paper-intent creation.

The adaptive selector must:

- Evaluate only enabled/eligible strategies.
- Rank opportunities across configured symbols using regime fit, aligned timeframe evidence, liquidity, spread and estimated fees/slippage.
- Explicitly mark strategy performance as `NOT_CALIBRATED` until walk-forward and untouched holdout evidence is available; the current fit score does not use validated historical performance.
- Select the strongest eligible strategy, or explicitly return **NO_TRADE**.
- Treat long-term analysis as advisory-only.
- Label heuristic fit scores as uncalibrated; never present them as win probabilities.
- Use realistic fees, spread, slippage and funding where relevant.
- Require reproducible walk-forward and untouched holdout testing before claiming strategy quality or profitability.
- Never force a trade because automation is enabled.

### Continuous Opportunity Engine — approved design, implementation not started

The design specification is `docs/specs/2026-10-31-continuous-opportunity-engine-design.md`.

The design defines three cooperating engines that extend the existing shared market-data and assessment foundation:

1. **Continuous Market Search Engine:** scans configured instruments using validated shared snapshots; discovers candidates and records scan health, freshness and coverage.
2. **Multi-Strategy Assessment Engine:** independently evaluates scalping, day trading, swing trading, position trading and long-term analysis. Strategy disagreement remains visible; long-term analysis is analysis-only.
3. **Multi-Opportunity Waiting Engine:** persists independent opportunities, including multiple distinct setups for one symbol; monitors trigger, invalidation, expiry and reassessment conditions; recovers after worker restart.

A trigger only starts fresh signal validation and independent Risk Manager review. It never directly creates a paper intent. PostgreSQL-backed state, idempotency, worker leases, audit history, bounded concurrency and fail-closed recovery are required.

**Status:** design document committed for user review; no feature implementation or runtime verification is claimed. The existing `signal_watcher.py` remains in-memory and `opportunity_intelligence.py` remains stateless until the implementation is completed and tested.

## Part 7 — Paper Trading, Portfolio Risk and Position Management

**Status: paper ledger, risk controls and protection foundation exist; further integrated verification remains open.**

- Strategy output is a proposal only and may proceed only to independent Risk Manager review.
- A triggered waiting opportunity must undergo fresh market/signal validation and independent Risk Manager review; trigger events are never order instructions.
- Opportunity records, state transitions, trigger events and worker claims must be durable and idempotent in PostgreSQL, with restart recovery and duplicate-processing protection.
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

**Continuous Opportunity Engine — design recorded, not implemented.**

- Design specification: `docs/specs/2026-10-31-continuous-opportunity-engine-design.md`.
- The design is for user review before implementation planning.
- No new opportunity schema, persistent waiting engine, trigger-to-Risk-Manager handoff or Android opportunity workflow is claimed as implemented.
- Existing in-memory/stateless component limitations remain until implementation and verification.

## Current implementation checkpoint — 2026-10-31

**Market Watcher / Multi-Timeframe Assessment / Adaptive Strategy selection — merged to the staging integration branch, not deployed.**

- PR #72 was squash-merged into `fix/futures-context-mainline` at commit `361d0c60aa4c39f99e9338b0eee758b0be95c568`.
- Backend/PostgreSQL CI at the tested PR head: **468 passed, 13 skipped**. ASGI, market-data mirror/regression and Android unit/extended-timeframe checks passed.
- A debug APK was built successfully. It targets the existing Render staging URL; it has not been installed or phone-verified.
- The additive `market_intelligence_reports` migration is applied and verified in `solo-ai-auto-trader-staging` only. The main Supabase project is unchanged.
- No Render deployment was performed. The current Render Free staging service stopped after SIGTERM at 15:41 UTC and its worker heartbeat expired. The service is not an always-on host for this workload.
- The new worker, scheduled scan, assessment API and Android visibility are implemented in the staging branch, but the Market Watcher is **not currently running on a host**.
- The adaptive selector produces ranked market candidates only. It does not yet hand candidates to the independent Risk Manager or create paper intents.
- Strategy performance remains `NOT_CALIBRATED`; fit scores are heuristic. Forex data/session support is not included.

### Next gates

1. Preserve zero spend; select and verify an always-on host only after confirming hard spending controls. No provider or paid plan is authorized.
2. Deploy the merged staging branch only after the hosting decision; verify worker heartbeat, scheduled scan publication, authenticated API response, and Android display.
3. Validate each strategy with realistic fees/slippage, walk-forward and untouched holdout tests.
4. Only then implement and test the proposal-to-Risk-Manager handoff and paper-position integration. Live order authorization remains separately disabled.

## Explicit limitations

- The shared worker currently uses Binance USDⓈ-M Futures market data in its Testnet configuration; Testnet observations are not production-market evidence.
- A Forex data/broker adapter and Forex session-calendar implementation are not established by this work.
- Strategy fit scores are heuristic, not calibrated confidence; profitability is unproven.
- The Market Watcher candidate stream is not yet connected to the Risk Manager or paper execution.
- No live exchange order authorization is part of this roadmap update.
