# Solo Research Learning, Graph Analysis and Specialist Boundaries

**Reviewed:** 2026-11-07

## Deterministic market intelligence

The shared market-intelligence pipeline validates closed candle histories and produces timeframe regime/conflict evidence, technical indicators and five independent strategy profiles (scalping, day, swing, position and long-term). The profiles preserve disagreements rather than majority-voting away conflicts. Long-term analysis remains analysis-only. Scores are heuristic and `NOT_CALIBRATED`.

Indicator values are descriptive evidence, not calibrated probabilities, forecasts, order instructions or risk approval. Invalid, stale, incomplete or gapped market history must be rejected or marked unavailable.

## Research-learning quality gate

`backend/strategy_learning_lab.py` accepts only paper-only research experiments with:
- immutable market-data manifests and source provenance;
- at least four out-of-sample validation folds;
- at least 30 validation trades;
- an explicit quality-qualified `EVALUATED` validation report with no failure reasons;
- fold counts and reported returns, positive-fold percentage and drawdown that reconcile to the underlying evidence;
- a separately recorded untouched final holdout that remains excluded from all learning rows.

The dataset contains validation-fold evidence only. It never copies untouched holdout values. Proposal weights are checked to reject risk, order, position, leverage, capital, allocation, stop, exposure, margin and other execution-control fields.

Independent evaluation must use a separate fingerprint and non-overlapping market-data window. Even a proposal that beats the baseline return without exceeding baseline drawdown receives `REVIEW_REQUIRED` only. Promotion remains `NOT_PROMOTED`; no automatic policy promotion, risk-setting update or order authorization exists.

## Graph relationship preparation

`backend/market_relationship_graph.py` builds a deterministic undirected graph from aligned closed-period per-symbol returns:
- canonical uppercase symbols, equal observation counts, minimum history and finite-number validation;
- Pearson correlation edges above a configured absolute threshold;
- positive and inverse relationship labels;
- node mean return, volatility, degree and degree centrality;
- deterministic input fingerprint and sorted output;
- constant-return series preserved as isolated nodes, without fabricated correlations.

This module prepares graph features for future Graph Attention Network research. It is not a trained GAT, neural inference engine, automated market forecast or trading policy. The graph has no order, risk-setting, strategy-promotion or live-exchange interface.

## Deterministic specialist protocol

`backend/research_agent_protocol.py` implements a versioned in-process protocol and deterministic supervisor for registered research handlers. It uses immutable market snapshots with symbol, market type, venue, source fingerprint and observation/expiry timestamps. It validates allowlisted roles, status, payload bounds and finite JSON-safe evidence; it rejects stale/mismatched inputs, duplicate identities and forbidden order/risk fields. Handler failures expose sanitized error types rather than private exception content.

Risk Guardian is deliberately not a specialist role. It remains the independent authority for paper-intent approval. Specialist outputs are evidence only and do not become risk permission.

The Android specialist-role catalogue is not evidence that fifteen autonomous agents are running. The current protocol does not provide a persistent message bus, distributed workers, LLM orchestration, MCP/A2A transport, production tracing or durable agent-message audit. Do not describe it as a production multi-agent platform.

## PPO / RL / GAT training boundary

PPO/RL/GAT training and serving are not implemented as running Solo services. The learning lab and graph preparation code define safe data and proposal boundaries only. Training remains blocked until all of the following are supported by inspectable evidence:
1. quality-qualified training datasets pass the research gate;
2. point-in-time feature provenance, reward calculations and feature versions are leakage-tested;
3. chronological training/validation partitions and independent, untouched evaluation windows are proven non-overlapping;
4. costs, slippage, funding, simulated fills, protection and portfolio accounting are realistic enough for the intended task;
5. a fixed deterministic baseline is beaten on independent data with acceptable drawdown and sufficient sample size;
6. candidate weights are immutable, reviewable, and cannot modify risk or execution controls.

Any future model must run offline first and may emit only candidate evidence for separate review. It cannot call exchange endpoints, create intents, modify risk settings, clear Emergency Stop or authorize paper actions. Deployment requires a separate review and explicit decision.

## Explicit safety invariants

- Runtime remains `PAPER_ONLY`.
- Every specialist, graph, learning and strategy output must retain `orders_authorized=false`.
- Risk Guardian retains exclusive authority to approve paper intents.
- Emergency Stop is never automatically cleared.
- Stale, mismatched, incomplete or invalid evidence fails closed.
- Holdout evidence remains untouched and excluded from learning.
- No model or agent can change risk limits, leverage, size, execution state or exchange permissions.
- No live orders or paid-service dependency are introduced by these research components.
