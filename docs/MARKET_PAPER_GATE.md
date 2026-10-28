# Market-to-paper safety gate

`app/engines/market_paper_gate.py` is the safety boundary between `MarketDataWorker` frames and an existing paper processor such as `TradingOrchestrator.process`.

Before forwarding a request, the gate requires:

- `FeedStatus.HEALTHY`, no unexpected order-authorization flag, and at least one closed candle;
- normalized symbol agreement between frame and request;
- finite positive and matching mark prices;
- timezone-aware, non-future, fresh worker/mark timestamps and a finite, fresh request market timestamp.

Any failed check returns `MARKET_DATA_BLOCKED` and does not call the processor. If the gate passes, the existing processor must still validate the signal, confirm the watcher trigger, apply deterministic risk limits, and write only a paper intent. The gate does not generate research candidates, derive prices, simulate fills, or place exchange orders.

Example:

```python
from app.engines.market_paper_gate import MarketPaperGate
from app.engines.orchestrator import TradingOrchestrator

orchestrator = TradingOrchestrator(paper_ledger)
gate = MarketPaperGate(orchestrator.process, max_age_seconds=90)
result = gate.process(market_frame, orchestration_request, now=current_epoch_seconds)
```

The market frame and orchestration request must describe the same symbol and mark price. A gate pass is not a profitability claim and does not enable live execution.
