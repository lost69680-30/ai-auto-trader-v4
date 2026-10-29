# Spot research evaluation safety contract

This document specifies a separate offline evaluation path for Spot research. It is not an execution service.

## Required invariants
- Read only historical OHLCV candles; do not request private account data or submit exchange requests.
- Long-only Spot exposure. No short positions, leverage, Futures mark price, or funding assumptions.
- Charge explicit per-side fee and slippage estimates on entries and exits. Label estimates as assumptions, not exchange-verified costs.
- Use signals only after their input candle is closed; simulate fills no earlier than the next candle open.
- Reject mixed symbols, non-finite or invalid OHLCV, non-monotonic timestamps, cadence gaps, and timestamps beyond the evaluation clock.
- Keep strategy and benchmark evaluation windows chronological. Reserve the final holdout before fold construction and never use it for tuning.
- Compare the strategy against cash/no-trade and buy-and-hold baselines using the same out-of-sample windows.
- Report trade count and an insufficient-sample warning. Do not imply a small sample establishes profitability.
- All results are research-only; `orders_authorized` must remain false.

## Scope boundary
This path must not remove the existing Futures-only guard in the paper service, fabricate Spot mark prices or funding fields, create paper orders/positions, or connect to order endpoints. Any later integration requires separate tests and explicit review.
