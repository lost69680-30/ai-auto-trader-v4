# Paper position sizing

`app/engines/paper_sizing.py` converts an already validated signal handoff into
an `OrchestrationRequest` for the existing watcher/risk/orchestrator path. It is
not a strategy and it does not authorize or submit exchange orders.

## Explicit inputs required

The caller must provide simulated equity, risk fraction, contract quantity step,
minimum quantity, risk limits, daily P&L, current open-position count, and a
current market tick. No account equity, exchange lot-size rule, leverage, funding
cost, or live account state is fetched or guessed by this module.

## Sizing rule

The raw quantity is the smaller of:

- `min(simulated_equity * risk_fraction, max_risk_per_trade) / abs(entry - stop)`
- `max_position_notional / entry`

Quantity is rounded down to the configured step. If the resulting quantity is
below the configured minimum, sizing fails closed. Risk fraction is constrained
to at most 2%; this is a conservative software guard, not a claim that 2% is a
suitable trading risk. Production paper runs should begin at a smaller fraction
and compare simulated fees/slippage with observed venue costs.

The helper rejects non-VALID handoffs, authorization flags, symbol/direction
mismatches, invalid stop geometry, future or stale ticks, and invalid sizing
inputs. The Risk Guardian still independently checks the resulting proposal.

## Limitation

This is a sizing adapter, not a completed autonomous strategy. Research data,
contract metadata, cost assumptions, daily P&L, and the orchestration factories
must still be wired by a service composition layer. A paper backtest or passing
test suite does not demonstrate positive expected returns.
