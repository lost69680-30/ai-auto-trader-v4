# Durable Paper Protection Engine

`app/engines/protection.py` provides a local-only protection service backed by the same SQLite database as `SQLitePaperLedger`. It cannot submit exchange orders and always returns `orders_authorized=False`.

## Emergency stop

- Activating the emergency stop persists the global stop state and reason in SQLite.
- In the same database transaction, every pending paper intent is recorded as cancelled. Cancelled intents stay cancelled even after the stop is reset.
- While active, the ledger rejects new intents and simulated fills. Reopening the database restores the active state.
- If activation is called without market prices, it halts new risk and cancels pending intents but leaves open positions untouched. It does not invent a price.
- If current market prices are supplied at activation, positions for those symbols are marked and paper-closed with the `EMERGENCY_STOP` reason. Positions whose symbols have no supplied price remain open and the stop stays active; subsequent market updates can finish the paper flatten.
- The stop cannot be reset while any paper positions remain open. A manual reset reason and non-backdated timestamp are required. Resetting does not uncancel old intents.

Example:

```python
from app.engines.paper_ledger import SQLitePaperLedger
from app.engines.protection import PaperProtectionEngine

with SQLitePaperLedger("var/paper-trading.sqlite3") as ledger:
    protection = PaperProtectionEngine(
        ledger,
        trailing_distance_pct=0.02,       # 2% trailing distance
        trailing_activation_pct=0.01,     # activate after 1% favorable move
    )

    # If no fresh quote is available, activate the stop without fabricating an exit price.
    # Supply a real timestamp from your validated feed/clock; this placeholder is illustrative.
    # protection.activate_emergency_stop(observed_at=..., reason="operator requested halt")

    # Later, pass prices from a real validated market-data adapter.
    # protection.process_market_update({"BTCUSDT": fresh_mark}, observed_at=fresh_timestamp)
```

## Automatic protection

For every `process_market_update` call:

1. If the emergency stop is active, accept only timestamp-valid supplied marks and paper-close positions for symbols with provided prices.
2. Otherwise, inspect stored marks first. If a process previously persisted a stop/target-crossing mark but exited before recording the close, recovery closes from that stored mark before replacing it with newer data.
3. Persist new marks and evaluate stop-loss and take-profit levels for both LONG and SHORT positions.
4. If configured, advance persisted favorable-price extrema and tighten trailing stops monotonically. A LONG trailing stop only moves up; a SHORT trailing stop only moves down. Extrema are stored to support restart recovery.
5. Record the simulated close and its reason (`STOP_LOSS`, `TAKE_PROFIT`, or `EMERGENCY_STOP`) in the audit trail.

Trailing distances are decimal fractions, so `0.02` means 2%. Set `trailing_distance_pct=None` to disable trailing stops. Fees and slippage are modeling assumptions supplied to the engine, not exchange-verified values.

## Integration and safety limitations

- The caller must reject stale, delayed, incomplete, out-of-order, or otherwise untrusted market feeds. The engine checks ordering/backdating but cannot independently prove quote freshness.
- Automatic exits operate on paper positions only. They are not exchange stop orders, and cannot protect a real-money account.
- A protective trigger is evaluated on each supplied mark; candle paths between observed marks are unknowable. Gap behavior is approximated by closing at the observed market price plus configured adverse slippage.
- Emergency activation without supplied prices is a halt/cancel, not a completed flatten. Operators must monitor remaining positions and provide current prices before resetting the stop.
- This engine does not model leverage, margin, liquidation, funding, partial fills, contract multipliers, or exchange outages.
