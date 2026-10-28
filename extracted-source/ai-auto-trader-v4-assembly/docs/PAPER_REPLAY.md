# Deterministic Paper Replay Harness

`app/engines/paper_replay.py` replays explicitly supplied event records against the real file-backed SQLite paper ledger and paper protection engine. It is an offline test utility, not a production worker or exchange adapter.

## Covered events

- `RecordIntentEvent`: records a paper-only intent and preserves duplicate/conflict behavior.
- `FillEvent`: simulates a fill using explicitly supplied market price, fee rate, and slippage assumption.
- `MarketUpdateEvent`: checks observation timestamp freshness before marking positions or evaluating simulated SL/TP/trailing exits.
- `EmergencyStopEvent`: activates the durable paper emergency stop and optionally supplies prices for simulated flattening.
- `RestartEvent`: closes and reopens the SQLite database connection, then reconstructs the protection engine to exercise persisted-state recovery.

## Safety and replay rules

- The replay clock must be finite, non-negative, and non-decreasing. A backwards event is rejected without ledger mutation.
- Market observations dated in the future, older than `max_market_age_seconds`, or older than the previous accepted observation are rejected before they can affect positions.
- The harness requires a file-backed database; `:memory:` cannot test real restart recovery.
- Repeated identical intents remain idempotent. Repeated fill records only return `ALREADY_FILLED` if the fill details, including timestamp and fee/slippage assumptions, match the stored fill. A changed replay record is a conflict, not a rewrite.
- Emergency-stop state remains persisted across `RestartEvent`; it continues blocking new paper intents and fills.
- All replay report and step objects have `orders_authorized=False`. No exchange adapter or network client is used.

## Example

```python
from app.engines.paper_replay import (
    MarketUpdateEvent,
    PaperReplayHarness,
    RecordIntentEvent,
    FillEvent,
    RestartEvent,
)

harness = PaperReplayHarness("/tmp/paper-replay.db", max_market_age_seconds=3)
report = harness.run([
    # Supply a PaperOrderIntent to RecordIntentEvent.
    # Then include a FillEvent, market updates, and RestartEvent as needed.
])
for step in report.steps:
    print(step.at, step.event_type, step.status, step.reason)
```

The harness does not generate strategies, invent entry/stop/target levels, or determine whether a strategy is profitable. Its purpose is to make deterministic safety/recovery scenarios reproducible. It also does not model realistic Futures margin, funding, liquidation, partial fills, or exchange latency.

## Test

From the repository root:

```bash
python -m pytest -q tests/test_paper_replay.py
```

The tests cover stale/future price rejection, stale prices not triggering protective exits, duplicate intent/fill behavior, SQLite restart recovery, protective exits, emergency-stop persistence, and out-of-order event rejection.
