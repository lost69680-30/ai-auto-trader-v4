# Scout, Scan, Boundary and Excursion Engines

## Scout and scan
`backend.setup_scout` evaluates timestamped OHLCV candle histories and flags range-edge approaches (early watch setups) and range breaks. It reports prior-range support/resistance, ATR-based illustrative stop-loss and take-profit boundaries, a transparent ranking score, and reason strings. The current candle is excluded from the prior range. Invalid, stale, future, mixed-symbol, non-finite, or inconsistent candles are rejected. The output is `WATCH_ONLY`; `orders_authorized` is always false.

`scan_markets` scans a symbol-to-candle-history mapping and reports per-symbol rejections. It does not fetch market data by itself; the caller must supply candles from a validated market-data adapter.

## Boundary planner
`app.engines.boundary_excursion.plan_boundaries` computes ATR-based risk boundaries, optionally respecting nearby support/resistance. Values are planning metadata only. If structure prevents a valid positive stop/target, the planner raises rather than fabricating levels.

## Excursion tracker
`update_excursion` tracks maximum favorable excursion (MFE), maximum adverse excursion (MAE), and R-multiples. Optional trailing stops ratchet only in the favorable direction; they never widen. Percentages in this API are expressed as percentage numbers (e.g. `1.0` means 1%).

## Safety boundaries
These modules have no exchange client, account access, or order-placement endpoint. A scout is not an entry instruction. Before any paper order can be proposed, the application must still validate market freshness, sizing, instrument rules, independent risk limits, and emergency-stop state. Missing or invalid risk validation must block the proposal. Live execution remains out of scope.
