# Solo strategy research cost sensitivity

## Purpose
The research runtime reports a validation-only cost-sensitivity diagnostic alongside the existing walk-forward quality assessment. It helps identify strategies whose historical net returns are sensitive to higher estimated transaction costs.

## Method
For each existing validation fold, the diagnostic replays the recorded simulated trades in chronological order using:
- baseline recorded estimated costs (1.0×);
- 1.5× all recorded estimated costs;
- 2.0× all recorded estimated costs.

Fees, slippage and funding are scaled together because the current simulator stores one per-trade combined cost value. The replay recomputes gross P&L, stressed costs, net P&L, win rate, mean net return, mean excess return against the simplified buy-and-hold benchmark, positive-fold percentage and maximum fold drawdown. Equity is floored at zero, matching the paper simulator; after equity is exhausted, later trades in that fold are not simulated.

## Evidence boundary
- Only walk-forward validation folds are read.
- The untouched final holdout is never accessed by the cost-sensitivity function.
- When a fold does not include its full trade-level paper ledger, the report is marked UNAVAILABLE; no per-trade stress metrics are inferred from aggregate totals.
- This diagnostic does not modify the existing validation_quality status, strategy selection, learning dataset, or strategy promotion decision.
- An EVALUATED cost-sensitivity result is not proof of future profitability, market-impact realism, or order-book execution quality.

## Current simulator limitations
The current backtester uses estimated fees/slippage/funding and simplified one-bar exits. Uniformly multiplying its combined per-trade cost is a sensitivity analysis, not a full spread/market-impact model. It does not independently estimate order-book depth, queue position, partial fills, latency, liquidation, stop/target intrabar sequencing, or dynamic funding events. Those require richer validated data before the cost model can be described as execution-realistic.

## Safety
All outputs are research diagnostics with orders_authorized=false. The report has no order, risk-setting, emergency-stop, or strategy-promotion interface. Risk Guardian remains the sole paper-intent authorization authority.
