# V4 public Spot market-data fallback (paper-only)

The USDⓈ-M Futures adapter remains unchanged. If Render returns HTTP 451 from `fapi.binance.com`, you may explicitly test the separate Binance public Spot feed:

- Set `PAPER_MARKET_DATA_MODE=spot` in the V4 Render service environment.
- Set `PAPER_MARKET_TESTNET=false` (or remove it) because the Spot data API is public production market data, not Futures Testnet.
- Keep `PAPER_API_TOKEN` in Render secrets. Never paste it into chat or the repository.
- Redeploy and inspect logs for `market data mode=SPOT_PUBLIC_ANALYSIS_ONLY` and `market feed status=HEALTHY`.
- This mode provides Spot candles and a Spot last-trade price for analysis. It is **not** Futures mark-price data and does not test Futures execution connectivity.
- If Spot requests also fail or candle validation is unhealthy, leave the feed offline and `orders_authorized=False`; do not bypass regional restrictions.

This application remains read-only and paper-only. A healthy feed never grants order authorization.
