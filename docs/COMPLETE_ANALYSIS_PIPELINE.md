# Complete analysis-to-paper pipeline coordinator

`app/engines/complete_analysis_pipeline.py` connects an already-computed `ResearchPipelineResult` to the downstream paper path:

`research candidate → explicit ATR plan inputs → ATR planner → signal handoff/validation → trigger watcher → independent Risk Guardian → paper ledger intent`.

## Required adapters

The coordinator intentionally requires two caller-supplied factories:

- `plan_request_factory(candidate)`: supplies reference mark and observation timestamp, closed candles, expiry, transaction-cost estimate and exchange tick size. These values must come from current validated data and configured exchange metadata; do not guess them.
- `orchestration_request_factory(candidate, handoff)`: supplies a proposal and configured risk limits, including position sizing and existing portfolio state. Risk Guardian independently checks notional, per-trade risk, daily loss, open-position limits and signal age.

The caller supplies current `MarketTick` values keyed by normalized symbol. Missing ticks, missing/invalid plans, failed validation, exceptions, duplicated candidates, and authorization flags fail closed. Exceptions are reported by type only to avoid leaking request data.

## Deliberate limitations

This coordinator does not itself fetch market data, derive the liquidity structure funding context, infer a tick size, choose account risk limits, size a trade, simulate a fill, or run a forever-loop. Those remain explicit integration/operations tasks. The paper ledger records intents; fills and protective exits are handled by the existing simulation/protection components. No live exchange orders are authorized.

## Suggested next verification gates

1. Add an integration fixture with known closed candles, scanner-ready market metadata and funding/structure inputs, and verify the complete path through `PAPER_ORDER_RECORDED` only when every gate passes.
2. Test invalid and stale feed data, missing structure/funding, no consensus, costs too high, invalid tick size, trigger not hit, risk veto, emergency stop, duplicate signal, restart and simulated SL/TP exits.
3. Run walk-forward backtests with realistic fee, spread, slippage and funding assumptions before treating research output as useful.
4. Only after paper soak tests, design a separately permissioned Binance Futures Testnet execution adapter and reconciliation flow. Live trading must remain disabled by default.
