# Rate-Limited Market Data Worker

`backend/market_worker.py` wraps the public read-only Binance USDⓈ-M Futures REST adapter in a continuously running polling loop. It is a feed-health and data-delivery component only: it does not generate orders, call the Risk Guardian, or mutate the paper ledger.

## Behavior

- Polls configured symbols sequentially for klines and mark-price context.
- Requires closed candles; an in-progress candle is never passed to consumers.
- Rejects stale or future-dated mark prices and stale/future closed candles.
- Checks closed candle open-time spacing against the configured interval and withholds all candles for a symbol if a gap is found.
- Rejects symbol mismatches and invalid candle timestamps.
- Exposes per-symbol `HEALTHY`, `STALE`, `GAP_DETECTED`, `DISCONNECTED`, `NO_CLOSED_CANDLES`, or `INVALID_DATA` status, with reasons and retry counts.
- Enforces a minimum one-second poll interval and uses capped exponential wait after network failures. Successful recovery resets backoff.
- Runs until a supplied `threading.Event` is set. Consumers can subscribe via `on_result`.
- Every frame and poll result explicitly carries `orders_authorized=False`.

## Example

```python
import threading
from backend.market_data_adapter import BinanceUsdMFuturesAdapter
from backend.market_worker import MarketDataWorker, MarketWorkerConfig

stop = threading.Event()
worker = MarketDataWorker(
    BinanceUsdMFuturesAdapter(),
    MarketWorkerConfig(
        symbols=("BTCUSDT", "ETHUSDT"),
        interval="1m",
        candle_limit=100,
        poll_interval_seconds=5,
        max_market_age_seconds=90,
    ),
)

# Run this in a managed backend worker thread/process. The callback should only
# pass HEALTHY frames onward; it must not bypass signal validation or risk checks.
worker.run_forever(stop, on_result=lambda result: print(result))
```

Call `stop.set()` from the service's shutdown path. The loop is a synchronous REST poller, not a WebSocket stream. For large symbol universes, do not poll every symbol every few seconds: use an exchange-weight budget, symbol batching/cache policy, and/or WebSocket market streams before scaling. API quotas and endpoint weights must be considered in deployment configuration.

## Current limitations

- The worker exposes validated frames to a callback. `app/engines/paper_runtime.py` consumes its polls and `app/engines/complete_analysis_pipeline.py` connects research to the downstream paper-intent path, but the concrete composition is still injected: there is no turnkey entry point that builds all research/plan/sizing requests from every raw market poll.
- WebSocket streaming, exchange server-clock synchronization, persistent health metrics/alerts, and production rate-limit accounting are not included. The worker provides local feed-health statuses, but not an operational feed-health SLA.
- Live outbound connectivity could not be verified from this workspace. Deterministic tests use a fake adapter.
- All execution remains paper-only; `orders_authorized` is always false.

## Test

```bash
python -m pytest -q tests/test_market_worker.py
```
