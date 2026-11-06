# Solo research learning, graph analysis and agent boundaries

## Current implementation

### Multi-timeframe intelligence
The worker's market-intelligence pipeline produces closed-candle timeframe assessments, validated indicators, regime/conflict evidence, and five separate strategy profiles. These are deterministic analysis components. Their scores are heuristic, not calibrated probabilities. A scan may correctly return NO_TRADE or ANALYSIS_ONLY for every symbol.

### Research learning gate
backend/strategy_learning_lab.py accepts only paper-only experiment records with:
- an immutable market-data manifest;
- at least four validation folds;
- at least 30 validation trades;
- an explicit quality-qualified EVALUATED validation report with no failure reasons;
- internally consistent validation trade counts, mean excess return, positive-fold percentage, and maximum drawdown;
- an untouched final holdout that remains excluded from learning rows.

The research dataset includes validation-fold evidence only. It does not contain holdout values. Proposal weights cannot contain risk, order, position, leverage, capital, allocation, stop, exposure, or related execution/risk terms. PPO, RL, GAT and specialist labels are proposal categories, not permission to change the system.

An independent evaluation must use a non-overlapping market-data window and a separate dataset fingerprint. A candidate that beats the baseline return without exceeding its drawdown receives REVIEW_REQUIRED only. Promotion remains NOT_PROMOTED; there is no automatic promotion or order authorization.

### Relationship graph preparation
backend/market_relationship_graph.py prepares an undirected graph from aligned closed-period per-symbol returns:
- strict canonical symbols, equal observation counts, minimum history and finite-number validation;
- Pearson correlation edges above a configured absolute threshold;
- positive and inverse relationship labels;
- node mean return, volatility, degree and degree centrality;
- deterministic input fingerprint and sorted output;
- constant-return series are retained as isolated nodes, never assigned fabricated correlations.

This is a deterministic graph-feature preparation step suitable for later GAT experiments. It is not a trained Graph Attention Network, neural inference, or an automated market forecast. It has no order, risk-setting, strategy-promotion, or live-exchange interface.

## Specialist agents and orchestration

The existing Android AI Agents screen intentionally labels most specialists as specified/planned and not running. The current backend has real market-analysis modules and strategy profiles, but it does not yet run fifteen autonomous LLM agents, a persistent A2A/MCP message bus, or a distributed specialist-agent runtime. Do not claim those agents are active.

The next safe implementation gate is a versioned evidence-message contract and deterministic supervisor that:
1. accepts only identified source evidence with timestamps, symbol, interval, venue and data fingerprint;
2. records each specialist's inputs, output, runtime and failure status;
3. rejects stale, duplicated, malformed or conflicting claims;
4. never aggregates heuristic scores into an order permission;
5. routes candidate research only to the existing independent Risk Guardian for separate paper-only review.

## PPO/RL training decision

PPO/RL training is deliberately not enabled. The current research database has no passed strategy-quality rows at the reviewed checkpoint. Most experiments are rejected, insufficient, or legacy records with missing validation-quality status. Training a policy now would overfit to weak or incomplete evidence.

Training remains blocked until:
- qualified training datasets pass the research gate above;
- features and reward calculations are versioned and leakage-tested;
- chronological training/validation partitions and independent untouched evaluation are proven non-overlapping;
- transaction costs, slippage, funding, fills and protection are realistic enough for the intended market;
- a fixed baseline is beaten on independent data with a bounded drawdown;
- model weights are immutable and cannot affect risk limits or execution authority.

The future PPO runtime must be an offline research process. It may emit candidate weights or scores for evaluation only. It cannot call exchange endpoints or authorize paper intents. Model deployment remains a manual, separately reviewed operation.

## Explicit product truth

- PAPER_ONLY remains mandatory.
- orders_authorized=false remains mandatory.
- Risk Guardian retains exclusive paper-intent approval.
- Emergency Stop is never auto-cleared.
- No live orders or paid service dependency are introduced by these research components.
- Graph relationships and model/agent scores are explanatory evidence, not calibrated probability or profit forecasts.
