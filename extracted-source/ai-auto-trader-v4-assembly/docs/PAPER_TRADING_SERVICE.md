# Paper trading cycle coordinator

`app.engines.paper_trading_service.PaperTradingService` coordinates one
`MarketPollResult` from the read-only market worker. For each unique symbol it:

1. rejects duplicate symbols and unexpected order-authorization flags;
2. asks a caller-supplied request factory to construct the existing typed
   orchestration request from that symbol's frame;
3. sends the frame/request pair through `MarketPaperGate`;
4. reports a per-symbol result without allowing one symbol's request-construction
   or processing exception to stop the remaining symbols.

The supplied gate processor should be `TradingOrchestrator.process`, backed by a
paper ledger. The orchestrator still requires a valid signal, confirmed watcher
trigger, and independent Risk Guardian approval before recording a paper intent.
The separate `CompleteAnalysisPipeline` bridges research candidates to planning,
signal handoff, watch, and orchestration, but needs explicit factories for trusted
price-plan inputs and sized request/account state.

This coordinator intentionally does **not** infer a trade direction or fabricate
missing scanner/research/price-plan evidence. The request factory is an explicit
integration boundary; automatically composing raw frames, research inputs, price-plan
metadata, request sizing and current portfolio state into one unattended entry point
remains outstanding. It does not submit exchange orders and every
report has `orders_authorized=False`.

Run the focused tests with:

```bash
pytest -q tests/test_paper_trading_service.py
```
