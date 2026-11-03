# Paper portfolio exposure diagnostics

`app.engines.portfolio_exposure.calculate_portfolio_exposure` is a read-only
summary over open `PaperPosition` records. It requires a positive finite equity
baseline, an aware evaluation clock and fresh positive finite mark prices.
Marks older than 30 seconds by default, future-dated marks beyond the two-second
clock tolerance, malformed positions or incomplete data fail closed; they are
not silently treated as zero exposure.

The report includes:
- gross exposure (sum of absolute marked position notionals);
- signed net exposure (long notionals less short notionals);
- gross and net exposure as percentages of the supplied equity baseline;
- per-asset gross notional, share of gross exposure and share of equity;
- top-asset gross share and the Herfindahl-Hirschman concentration index (HHI).

An empty portfolio reports zero gross/net exposure but leaves concentration
metrics undefined because there are no assets to compare. All values are
descriptive; no concentration threshold or order decision is applied.

This module does not size positions, modify risk limits, block or authorize
orders, or replace the independent Risk Guardian. Its reports carry
`orders_authorized=False`. The Risk Guardian remains the sole authority for
risk checks and all paper execution decisions.
