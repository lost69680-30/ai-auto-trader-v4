# AI Auto Trader v4 Architecture and Readiness

## Current implemented flow

1. **Public market data** — `backend/market_data_adapter.py` parses Binance USDⓈ-M Futures public REST market metadata, 24-hour stats, top-of-book, klines, and mark/index/funding context. It does not fetch account state or submit orders.
2. **REST worker** — `backend/market_worker.py` polls an explicit symbol list, checks freshness, closed candles, and gaps, tracks feed status, and uses capped backoff. Consumer/poll-cycle exceptions are logged and retried. No WebSocket stream, server-clock synchronisation, feed-health SLA, or full request-weight scheduler is present.
3. **Research** — `backend/scanner.py`, `backend/opportunity_intelligence.py`, `backend/strategy_research.py`, and `backend/liquidity_structure.py` are joined by `app/engines/research_pipeline.py`. These components provide structured candidate evidence and a fail-closed `NO_TRADE` outcome; they do not establish a profitable forecast.
4. **Price planning and validation** — `app/engines/price_planner.py` derives a candidate plan from explicit closed candles, a reference price/time, exchange tick size, expiry, and cost assumptions. `app/engines/signal_handoff.py` sends a reviewed candidate through signal validation, then requests a watcher.
5. **Trigger and risk** — `app/engines/paper_signal_cycle.py` checks the trigger before `app/engines/orchestrator.py` applies the independent `app/core/risk_guardian.py` gate. The guardian validates finite inputs and recomputes stop-distance risk conservatively. It does not place orders.
6. **Persistent paper execution** — `app/engines/paper_ledger.py` records idempotent paper intents, simulated fills, positions, marks, P&L, emergency state, and audit events in SQLite. `app/engines/protection.py` simulates stop-loss/take-profit, optional trailing stops, emergency-stop exits, and recovery.
7. **Coordinator/runtime** — `app/engines/complete_analysis_pipeline.py` connects a research result to planning, signal handoff, trigger watch, and paper intent. `app/engines/paper_runtime.py` wraps healthy market polls, an injected analysis callback, simulated fills, and protective updates. It deliberately requires the caller to supply verified market configuration, costs, sizing, limits, and portfolio state.

## Safety controls strengthened in the latest audit

- Reject NaN, infinity, booleans in numeric fields, invalid risk limits, malformed timestamps, and invalid open-position counts.
- Recompute stop-distance risk as `quantity × abs(entry_price − stop_loss)` and compare the greater of the supplied estimate and recomputed risk to the trade risk cap.
- Reject duplicate symbol frames, stale polls, frame/poll timestamp mismatches, malformed candle evidence, and quotes observed before the pending intent.
- Preserve source market-observation timestamps in protection marks.
- Keep a transient analysis, polling, or consumer callback exception from silently terminating the continuous worker loop; logs must be monitored.

## Deliberate boundaries and missing work

- **No exchange execution adapter:** No Binance Testnet or live order placement, cancellation, order/fill reconciliation, or API-key handling exists. Do not interpret `APPROVED` as permission to send an order.
- **No self-contained composition layer:** the complete coordinator requires injected plan/sized-request factories, and the runtime requires an injected analysis handler. A trusted concrete entry point/config loader is still needed for unattended operation.
- **Simplified Futures accounting:** quantity × price assumes linear unit exposure. The model lacks exchange-specific contract multipliers, funding payments, leverage/margin, maintenance margin, liquidation, partial fills, and full portfolio netting.
- **Market-data operations:** current polling is REST-based and per symbol. Do not poll every eligible Futures contract at a short interval until request-weight budgeting, caching, and/or WebSocket streaming are implemented and verified. No live outbound connectivity smoke test was established here.
- **Product deployment:** no production REST/WebSocket API, authentication/session management, cloud deployment, secrets manager, monitoring/alerting/notifications, Android frontend, Gradle build, or APK is included.
- **Advanced AI:** PPO/RL, GAT, CTDE, and MCP/A2A runtime/training are future research directions, not implemented features or a guarantee of profit. Use a transparent baseline and out-of-sample validation before adding them.

## Gates before considering Testnet

1. Add a versioned runtime config with explicit symbol allowlist, exchange filters, transaction costs, clock policy, per-trade/daily/portfolio risk, and paper-account state.
2. Connect adapter → worker → scanner/research → planner → handoff/watcher → sizing/orchestrator → ledger/protection through a reproducible entry point.
3. Run multi-day paper/replay and restart drills, verify audit consistency and emergency stop, and inspect metrics for feed freshness, rejected signals, fills, drawdown, fees/slippage, and protection exits.
4. Implement a separately permissioned Testnet adapter with exchange-filter compliance, order lifecycle idempotency, cancellation/reconciliation, disconnect and partial-fill handling, plus comprehensive contract tests.
5. Only after stable Testnet and manual approval, evaluate any next execution scope. Real-money mode must remain disabled until those controls and separate approval exist.

`PROJECT_COMPLETENESS_AUDIT.md` is the detailed verification report. Passing the local tests shows only the behaviors covered by those tests, not profitability or production readiness.
