# Stateless multi-timeframe candle mode

Solo retrieves historical candles on demand from Binance public REST endpoints and keeps the working window in process memory. Live market updates continue through the existing validated worker/feed path. Raw candle rows are not written to Supabase/PostgreSQL when stateless mode is active.

## Default behavior

- PAPER_CANDLE_ARCHIVE_ENABLED defaults to false.
- The shared market worker does not instantiate the PostgreSQL candle archive, backfill candle history, or write new raw candle rows in the default mode.
- The authenticated /api/v1/markets/history endpoint retrieves bounded one-minute pages directly from the configured Futures Testnet public REST source.
- The normal chart endpoint continues to request its configured intervals directly from the exchange.
- The API caps a historical request at 10,000 one-minute candles and pages upstream requests in chunks of at most 1,500.
- Gaps, duplicate timestamps, mismatched symbols, unclosed candles, invalid timestamps, and source failures are rejected; the API does not invent missing history.
- On process restart, historical context is fetched again. No local RAM candle cache is treated as durable.
- Research outcomes, paper positions, fills, risk decisions, and audit records remain in durable storage; this change only removes raw candle persistence.

## Optional legacy archive

Set PAPER_CANDLE_ARCHIVE_ENABLED=true only when a durable raw-candle archive is intentionally required. The archive remains Testnet-only and requires PAPER_CANDLE_INTERVAL=1m. Existing archived rows are not deleted by this change.

## Safety

All market history responses remain PAPER_ONLY with orders_authorized=false. The change does not add exchange account access, signed requests, order placement, or a path around the independent Risk Guardian.
