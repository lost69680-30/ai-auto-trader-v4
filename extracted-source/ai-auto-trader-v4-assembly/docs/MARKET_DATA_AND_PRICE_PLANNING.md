# Public Market Data Adapter and ATR Price Planning

## Scope

`backend/market_data_adapter.py` is a read-only public REST adapter for Binance USDⓈ-M Futures. It does not need API credentials and has no account, order placement, cancellation, leverage, or position endpoint.

Supported calls:

- `fetch_market_universe()` joins exchange contract metadata, rolling 24-hour ticker statistics, and best bid/ask to produce records usable by `backend.scanner`.
- `fetch_klines(symbol, interval, limit)` parses OHLCV bars, preserves exchange timestamps, checks OHLC geometry and strictly increasing open times, and marks the still-forming bar as open.
- `fetch_mark_price(symbol)` returns mark price, index price, latest funding rate, next funding time, and the exchange-supplied observation time.
- `testnet=True` selects the public Futures Testnet REST base URL; `base_url` can be set explicitly for a controlled environment.

Official endpoint references: [Binance Developer Documentation](https://developers.binance.com/en/docs/introduction) and [USDⓈ-M Futures REST API catalog](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/convert). Endpoint paths in the client are public market-data paths (`/fapi/v1/exchangeInfo`, `/fapi/v1/ticker/24hr`, `/fapi/v1/ticker/bookTicker`, `/fapi/v1/klines`, `/fapi/v1/premiumIndex`).

The adapter uses Python's standard-library `urllib`; no additional runtime dependency is required. Tests inject a deterministic transport to check payload handling without network access. An outbound live read-only API smoke check did not complete in this workspace, so actual connectivity from the eventual deployment environment remains unverified.

## Price planning

`app/engines/price_planner.py` takes an explicit current reference price and timestamp, symbol, direction, closed OHLCV candles, expiry, tick size, and estimated round-trip cost. It computes a simple ATR over recent closed candles, then calculates directional stop/target distances, rounds values to tick increments, builds a trigger rule, and calculates cost-adjusted reward-to-risk.

The planner returns `READY` plus `SignalPricePlan` only when:

- Reference price and closed-candle history are fresh enough.
- Candle symbols, timestamps, OHLC geometry and history ordering are coherent.
- Enough closed candles exist for the configured ATR period.
- Expiry is still in the future and transaction costs stay within the configured cap.
- The rounded price geometry is valid and net reward-to-risk exceeds the configured minimum.

Otherwise, it returns `NO_PLAN`. A currently forming candle from REST is excluded from ATR rather than treated as a closed candle. The ATR defaults (`period=14`, stop distance `1.5 × ATR`, target `2 × stop distance`, trigger buffer `0.1 × ATR`) are transparent engineering defaults, not optimized trading parameters. The cost estimate must be supplied by the caller and must account for realistic fees/slippage; it is not fetched or guessed by the planner.

The resulting `SignalPricePlan` is compatible with `app/engines/signal_handoff.py`; passing that handoff still requires a research `REVIEW`, validated strategy votes, a watcher trigger, and independent Risk Guardian approval before a paper intent is recorded. `orders_authorized` remains false throughout.

## Safe usage example

```python
from datetime import datetime, timedelta, timezone
from backend.market_data_adapter import BinanceUsdMFuturesAdapter
from app.engines.price_planner import PricePlanRequest, plan_prices

market = BinanceUsdMFuturesAdapter(timeout_seconds=5.0)
klines = market.fetch_klines("BTCUSDT", interval="1m", limit=100)
mark = market.fetch_mark_price("BTCUSDT")
now = datetime.now(timezone.utc)

result = plan_prices(PricePlanRequest(
    signal_id="candidate-20261010-001",
    symbol="BTCUSDT",
    direction="LONG",  # supplied by the research pipeline, not inferred here
    reference_price=mark.mark_price,
    reference_price_at=mark.observed_at,
    candles=klines,
    now=now,
    expires_at=now + timedelta(minutes=2),
    estimated_round_trip_cost_pct=0.10,  # replace with calibrated fee + slippage estimate
    tick_size=0.10,  # must come from current exchange symbol filters in production
))

if result.status == "READY":
    print(result.plan)
else:
    print("NO_PLAN:", result.reason)
```

The example demonstrates read-only retrieval and candidate planning only. The hard-coded tick size is illustrative; production code must obtain and validate the symbol's current `PRICE_FILTER.tickSize` from exchange metadata. The current adapter exposes exchange contract metadata but does not yet parse/cache every symbol filter, so a production integration must add that mapping before passing real orders to any execution adapter.

## Important operational limitations

- This adapter is REST-only. A separate `backend/market_worker.py` provides polling, retry/backoff, feed-health states and candle-gap checks; WebSocket streaming, ping/reconnect handling, production request-weight scheduling and durable feed-health SLA are not yet implemented.
- Full-universe REST calls are rate-limited. They must be cached and scheduled conservatively; do not poll all symbols on every tight strategy loop. A production continuous scanner should use a rate-limited snapshot cycle and a market-data stream where appropriate.
- The adapter alone does not provide a feed-health SLA or server-clock synchronization. Candle-gap detection and per-symbol feed-health statuses are provided by the separate worker. No automatic fallback between production and testnet is implemented.
- The adapter timestamps joined ticker/book snapshots at local fetch completion; it cannot prove that every upstream component had matching event time.
- The current smoke check could not reach Binance from this workspace. The fixture tests prove parser behavior, not live connectivity or exchange availability.
- ATR price planning is a baseline heuristic. It neither forecasts returns nor demonstrates profitability. Run realistic walk-forward/backtests and paper replay before Testnet integration.
