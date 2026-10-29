# Spot Research Mode

## Scope

Spot research is an authenticated, read-only analysis path. It uses public Spot exchange metadata, 24-hour ticker/book snapshots, and closed OHLCV candles. It does not request Futures mark prices or funding rates and does not submit, authorize, or simulate orders.

When `PAPER_MARKET_DATA_MODE=spot` is configured, the existing read-only market poller uses `SpotMarketDataWorker`, whose frames contain `last_price` and `last_observed_at`; they deliberately have no `mark_price` or `funding_rate` fields. WebSocket events label Spot observations with `market_type: "SPOT"` and `last_price`. Futures events retain the existing `mark_price` contract.

## Research endpoint

`GET /api/v1/spot/research` requires the existing `Authorization: Bearer <PAPER_API_TOKEN>` header. It is available only when Spot market mode is enabled. The endpoint returns scanner eligibility, OHLCV-based structure candidates, evidence, data rejections, and `orders_authorized: false`.

The scanner and structure scores are screening heuristics, not probabilities of profit or trade recommendations. Spot does not support short selling in this research path; a `SHORT` structure pattern is descriptive only. No risk sizing, price-plan handoff, paper order creation, or live execution is connected to this endpoint.

## Fail-closed checks

- Futures scanner configuration is rejected by Spot research configuration.
- Non-Spot contracts, missing/stale/gapped candles, malformed prices, duplicate symbols, and non-finite values are rejected.
- The Spot worker never requests Futures mark-price or funding endpoints.
- The WebSocket schema validates separate Spot and Futures price/timestamp fields.
- Every research response and stream event explicitly sets `orders_authorized: false`.

## Configuration notes

- `PAPER_MARKET_DATA_MODE=spot` selects public production Spot market data; `PAPER_MARKET_TESTNET` must remain disabled for this mode.
- `PAPER_MARKET_SYMBOLS` limits symbols considered by the poller and research endpoint.
- `PAPER_MARKET_INTERVAL` selects the polling candle interval supported by the Spot worker.
- `PAPER_ANALYSIS_CANDLE_LIMIT` controls candle history used for the research endpoint (50–500).
- `estimated_round_trip_fee_pct` is configurable in code and defaults to zero; spread is included from the public order book. A zero fee estimate is not a realistic profitability assumption.

## Not yet implemented

This is not a complete Spot paper-trading pipeline. The Futures-oriented paper service remains blocked in Spot mode, by design. Spot risk sizing, paper fills/ledger positions, Spot-specific SL/TP and protection, walk-forward performance validation, and UI rendering for the new `last_price` stream field must be completed and independently tested before claiming end-to-end Spot paper simulation. No merge or deployment is implied by this document.
