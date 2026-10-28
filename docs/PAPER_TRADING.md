# Persistent Paper Trading Ledger

`app/engines/paper_ledger.py` is the local paper-account persistence layer. It uses Python's standard-library `sqlite3` and contains no exchange client, API credentials, or network order submission.

## Scope

The ledger durably stores:

- Paper-order intents recorded after the orchestrator's validation and Risk Guardian checks.
- One simulated entry fill per proposal, including market price, modeled fill price, fee, slippage assumptions, and timestamp.
- Positions, marks, protection levels, fees, closed status, and realized P&L.
- Emergency-stop state, cancelled-intent records, favorable price extrema for trailing stops, and audit events.

Open a file-backed database to preserve state across restarts:

```python
from app.engines.paper_ledger import SQLitePaperLedger
from app.engines.orchestrator import TradingOrchestrator
from app.engines.protection import PaperProtectionEngine

ledger = SQLitePaperLedger("var/paper-trading.sqlite3")
orchestrator = TradingOrchestrator(ledger)
protection = PaperProtectionEngine(
    ledger,
    trailing_distance_pct=0.02,       # 2% trailing distance
    trailing_activation_pct=0.01,     # activate after 1% favorable move
)

# Build each request from validated, timestamped market data.
# result = orchestrator.process(request)
# Only if result.status == "PAPER_ORDER_RECORDED":
#     ledger.simulate_fill(result.proposal_id, market_price=fresh_price, filled_at=fresh_timestamp)
# On each market update:
#     protection.process_market_update({"BTCUSDT": fresh_mark}, observed_at=fresh_timestamp)

ledger.close()
```

Public request/market-data adapters, a REST polling worker, a complete-analysis coordinator, and a paper runtime wrapper exist as separate components. A deployment-ready composition layer must still connect them using verified configuration and current portfolio state. Never fabricate live prices or timestamps in a deployed workflow. The default fill/close fee (`0.0004`) and slippage (`2` basis points) are illustrative assumptions, not exchange-verified values.

## Simulation rules

- Long entry simulation applies adverse slippage upward; short entry simulation applies it downward.
- A fill is refused if the simulated fill price is already outside the proposal's stop-loss/take-profit geometry.
- Long unrealized P&L is `(mark - entry) × quantity`; short unrealized P&L is `(entry - mark) × quantity`.
- Closing applies configured adverse exit slippage, subtracts opening and closing fees, and persists net realized P&L.
- Repeated intents/fills are idempotent; conflicting reuse of an ID is rejected.
- Back-dated fills/closes and stale mark updates are rejected. Mark updates roll back if any matching position would receive an older mark.
- Automatic stop-loss/take-profit exits, optional trailing-stop tightening, emergency-stop behavior and protection reason audit records are implemented in `app/engines/protection.py`; see `docs/PROTECTION_ENGINE.md` for precise semantics.
- Simulated result objects expose `orders_authorized=False`; the ledger's read-only property is always false.

## Explicit limitations

This is not a production-grade futures simulator. It does not model contract multipliers, leverage/margin, funding payments, liquidation, partial fills, realistic order-book spread, latency, exchange filters, portfolio netting, or complete account equity/margin. Protection triggers on supplied marks and is not an exchange-managed stop order. The caller must validate feed freshness and keep Risk Guardian checks mandatory before recording intents. The current runtime still relies on injected analysis/request factories; a fully configured unattended service, operational monitoring, and reconciliation drills remain outstanding.

Treat the SQLite file as sensitive trading history. Use strict filesystem permissions and controlled backups; use an access-controlled managed database before multi-user/cloud deployment.
