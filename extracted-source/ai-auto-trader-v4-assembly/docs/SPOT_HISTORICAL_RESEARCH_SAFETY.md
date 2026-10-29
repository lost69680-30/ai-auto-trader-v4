# Historical Spot research safety contract

- Public unauthenticated market data only; no API keys or account endpoints.
- Requests are bounded to 30–1500 candles and a fixed allowlist of Spot intervals.
- Only closed candles enter the offline baseline; the backtest validates timestamps, cadence, OHLCV, and future data.
- Output is research-only: `orders_authorized=false` and `positions_created=false`; no order, position, ledger, or exchange mutation occurs.
- The fixed moving-average baseline and assumed fees/slippage are not calibrated forecasts and do not establish profitability.
- Any API exposure must remain behind the existing bearer-token API and fail closed on provider errors.


## API exposure

The authenticated read-only API exposes `GET /api/v1/spot/backtest?symbol=BTCUSDT&interval=5m&limit=500`. The symbol must belong to `PAPER_MARKET_SYMBOLS`; invalid parameters are rejected, and provider or validation failures fail closed. The endpoint is enabled only when `PAPER_MARKET_DATA_MODE=spot`. It does not create orders, positions, or ledger entries.
