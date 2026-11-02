# External Strategy Research and Memory

This is a research-only intake and evaluation path. Public leaderboard records,
including Trader.AI records, are idea sources—not proof of profitability and
not instructions to copy or activate a strategy.

## Intake and provenance

`backend.external_strategy_intelligence` preserves the source name, absolute
source URL, source record ID, capture/publication timestamps, market scope,
symbols, timeframes, indicators, published performance sample/window,
methodology and any explicitly supplied rule specification.

Published metrics are preserved as source evidence. They are never converted
into probabilities, expected returns or calibrated confidence. A leaderboard
record without published rules remains `IDEA_ONLY`; proprietary rules are
never inferred from scores or rankings. A candidate requires a sufficient
sample, documented methodology, explicit supported rules, and market/timeframe
scope. Candidate readiness means only that it can be reproduced in the
research harness.

## Supported reproducible rules

The bounded rule DSL currently supports:
- `SMA_CROSS`: integer fast and slow periods, fast smaller than slow.
- `MOMENTUM`: integer lookback and positive return threshold.
- `BREAKOUT`: integer lookback and non-negative breakout buffer.
- `RSI_THRESHOLD`: integer period and ordered oversold/overbought levels.

The current simulator supports only an explicitly declared `ONE_BAR` exit
policy for external candidates. Other published exit rules are rejected rather
than silently approximated. A future extension must implement and test an exit
rule before accepting that rule family for reproducible evaluation.

## Evaluation and degradation

External candidates run through the same deterministic offline simulator with
explicit fee, slippage and funding assumptions. Walk-forward validation uses
chronological, disjoint validation windows and a separately reserved final
holdout. The holdout is reported but never used for selection. The harness
does not optimize parameters.

The decay diagnostic compares recent validation-fold excess returns with
earlier validation folds. It is descriptive, not a calibrated signal or an
automatic rejection/promotion rule. Insufficient folds are reported as
`INSUFFICIENT_FOLDS`.

## Research memory

`backend.strategy_research_memory` stores immutable experiment versions in
`paper_ledger.strategy_research_experiments`. The migration
`supabase/migrations/20261102184500_create_strategy_research_memory.sql`
adds a private append-only table with provenance, candidate fingerprint,
experiment/config versions, dataset window, assumptions, outcomes and failure
reasons. Idempotency keys and candidate/version uniqueness prevent duplicate
experiments. Database triggers reject update/delete operations.

The migration is code only until separately approved and applied. It has not
been applied to the user's Supabase project by this work.

## Safety boundaries and current limitations

- Every candidate, report and experiment has `orders_authorized=False`.
- No external strategy can invoke execution, change risk settings, or bypass
  the independent Risk Guardian.
- No automatic copy, promotion, deployment, exchange order, or paid Trader.AI
  integration is included.
- PPO/RL, GAT and specialist-agent training runtimes are not implemented by
  this module; this foundation only creates reproducible, provenance-backed
  research evidence for later isolated learning work.
- Published performance evidence is not independently verified merely by
  ingesting it. Internal validation is required before any future promotion.
