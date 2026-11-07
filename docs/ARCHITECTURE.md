# Solo Architecture and Current Readiness

**Reviewed:** 2026-11-07  
**Scope:** Source-level architecture across the current backend integration and Android integration branches. This document distinguishes implemented code, opt-in runtime behavior, CI evidence, and operational acceptance.

## Product and safety contract

Solo is a market-analysis and paper-research system. It is not a live trading bot.

- Runtime mode must remain `PAPER_ONLY`; every research, strategy, graph and specialist output carries `orders_authorized=false`.
- There is no live exchange order-placement interface in this architecture.
- The independent Risk Guardian is the only authority that may approve a paper intent. Research, indicators, strategies, specialist handlers and learning proposals cannot bypass it.
- AI-produced weights or settings cannot change risk limits, leverage, position sizing, Emergency Stop or execution controls.
- Emergency Stop is never automatically cleared. Stale, missing, malformed, uncertain or contradictory evidence fails closed.
- Credentials remain in backend secret storage and are not embedded in the Android application, research artifacts or agent payloads.

## Current market and paper-analysis flow

1. **Public Testnet market data** — the shared adapter uses explicitly scoped Binance Spot Testnet and Binance USDⓈ-M Futures Testnet sources. Venue identity is retained; Spot data is not interpreted as Futures mark-price or funding context. The adapters are read-only.
2. **Shared supervised worker** — one shared market-data worker supplies validated snapshots and candles to market analysis and durable scheduled jobs. It checks candle closure, freshness, gaps and source identity. Opportunity scanning reuses this source; it does not create a second exchange polling loop.
3. **Market assessment and Indicator Intelligence** — validated closed-candle series produce timeframe regime, trend, momentum, volatility, structure, support/resistance, candle-pattern and conflict evidence. Indicator Intelligence calculates EMA20/50, RSI14, MACD 12/26/9, Bollinger Bands, ATR14/ATR%, VWAP and prior-20-candle relative volume. Invalid, stale, incomplete or gapped histories are rejected or reported unavailable.
4. **Independent Nexus strategy assessment** — scalping, day, swing, position and long-term profiles are assessed independently. Indicator context is attached as evidence, not majority-voted into one direction. Long-term assessment remains analysis-only. Scores are heuristic and `NOT_CALIBRATED`, not win probabilities.
5. **Durable Continuous Opportunity Engine** — a PostgreSQL registry stores independent opportunities, versioned assessments, price-plan evidence, watch conditions, append-only events and worker claims. Spot and Futures coordinators are venue-isolated. Reassessment uses the opportunity's own timeframe and fresh closed candles. Expiry, invalidation, duplicate delivery and lease reclamation are handled fail-closed.
6. **Fresh trigger and paper-risk handoff** — a triggered Futures opportunity is reloaded and reassessed with fresh market/candle evidence and the independent strategy profiles. The handoff enters the existing signal-validation and paper-service path, then the PaperSignalCycle and Risk Guardian. Intent state is only advanced when the paper ledger confirms the result. Spot remains research-only.
7. **Paper ledger and protection** — the paper service records idempotent intents, simulated fills, positions, marks, P&L, Emergency Stop state and audit events. Protection logic simulates stop-loss/take-profit and permitted trailing behavior. This is simulation, not exchange-side protection.
8. **Authenticated read-only opportunity API** — list/detail/event/assessment/evidence endpoints expose opportunity research. There are no opportunity mutation endpoints.

## Runtime activation and hosting constraints

The source supports scheduled research, but runtime activation is deliberately opt-in:

- `PAPER_MARKET_INTELLIGENCE_ENABLED` defaults to `false`.
- `PAPER_STRATEGY_RESEARCH_ENABLED` defaults to `false`.
- `PAPER_OPPORTUNITY_PAPER_HANDOFF_ENABLED` defaults to `false`.
- Raw candle archiving remains separately opt-in; stateless bounded candle retrieval is supported without making permanent candle archives mandatory.

Do not enable these flags merely to make the dashboard look active. Read-only research activation and paper handoff are separate decisions. Handoff must remain disabled unless its specific staging acceptance gate is satisfied.

The independent external watchdog checks worker liveness and an independent API/database readiness endpoint. The scheduler watchdog distinguishes a stalled job runner from a stale market feed or a failed individual job. It must not restart a healthy worker or treat a transient database health-read exception as confirmed failure. The user-provided November 6 run confirms the healthy/no-restart path; a real confirmed-failure restart-and-recovery event has not yet been accepted.

**$0 hosting limitation:** the current Render Free service can sleep and does not provide guaranteed continuous worker compute. GitHub Actions is useful for CI and manually dispatched checks, not a replacement for a real-time worker or guaranteed scheduler. Do not use keep-alive traffic, hidden background polling, paid resources or usage that can incur charges to simulate always-on hosting. When the host sleeps or the worker heartbeat expires, the UI and API must report degraded/stale state.

## Strategy research and evaluation

The strategy-research runtime compares fixed candidate families using chronological walk-forward folds, a distinct untouched final holdout, market-data lineage and explicit benchmark comparisons. It does not tune or promote parameters automatically.

Validation-only cost sensitivity recomputes metrics from validation-fold trade-level ledgers at ordered cost multipliers. It never reads untouched holdout data. If trade-level evidence is missing, the report is `UNAVAILABLE`; aggregate totals must not be used to fabricate trade-level stress results.

These evaluations use simplified simulator assumptions. They do not fully model order-book depth, market impact, liquidation, exchange-specific contract multipliers, full Futures funding/margin accounting or actual venue fills. Backtest and stress reports are research evidence, not a profitability forecast.

## Graph and learning research

- The relationship graph module creates deterministic correlation edges and node statistics from aligned closed-period return arrays. It is preparation for future GAT research, not a trained Graph Attention Network or forecast.
- The learning lab accepts only quality-qualified walk-forward validation evidence, records data lineage and excludes untouched holdout values. A proposal can receive `REVIEW_REQUIRED` only; it cannot auto-promote a strategy or alter Risk Guardian settings.
- The deterministic specialist protocol is an in-process research supervisor over immutable, fingerprinted snapshots. It validates timestamps, venue/symbol identity, role allowlists, payload size and safety fields; it sanitizes failures and rejects risk/execution controls.
- These are not fifteen autonomous LLM agents, a persistent distributed message bus, or a production MCP/A2A network.
- PPO/RL/GAT training and inference are not implemented as running production services. They remain offline research-only and gated on qualified datasets, leakage-safe partitions, realistic simulation and independent evaluation.

## Android and TradingView chart boundary

The Android integration branch contains the TradingView Lightweight Charts Android wrapper. It renders Solo-provided OHLC candles after local validation of timestamps, numeric values, OHLC consistency, ordering and supported price precision. It has no order or execution interface.

TradingView is the chart-rendering library here, not Solo's market-data provider. Candle history and live observations come through Solo's own configured market-data/API path. Do not scrape TradingView, extract TradingView indicator values, or treat a TradingView widget as a custom-data API. The 5.2.1 Android wrapper dependency upgrade is in the draft Android cost-sensitivity PR and remains subject to its current CI and physical-device acceptance.

## Deliberate exclusions and remaining acceptance

- No live Binance order adapter, order placement, cancellation, live fill reconciliation or live account trading.
- No Forex adapter or verified Forex session-calendar implementation.
- No autonomous LLM-agent network, distributed A2A/MCP bus, trained PPO/RL policy or trained GAT model.
- No claim of guaranteed 24/7 processing on the current free host.
- Passing GitHub CI proves only the tested source and disposable-CI behavior. It does not establish that the current Render deployment runs the latest source, that a long real-market soak has passed, or that an APK has been installed and accepted on the user's phone.

Before widening runtime activation, complete and record: actual staging deployment/source identity; worker/API readiness; a real confirmed-failure recovery drill; scheduler and duplicate-delivery verification; backup/restore and rollback evidence; multi-day paper/replay soak; and Android physical-device chart, notification, freshness and safety acceptance.

**Permanent boundary:** `PAPER_ONLY`; `orders_authorized=false`; no live orders; independent Risk Guardian authority; Emergency Stop never auto-cleared; no paid infrastructure or accidental billing.
