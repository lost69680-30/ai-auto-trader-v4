# Solo deterministic specialist research protocol

## Purpose and maturity
The specialist protocol provides a deterministic supervisor for running registered, read-only analysis handlers over a common, immutable market-data snapshot. It is a local research orchestration contract; it is not an autonomous LLM agent platform, distributed A2A/MCP bus, production agent service, or execution system.

## Message contract
Each specialist evidence message is protocol version 1.0 and carries:
- canonical symbol and market type;
- venue identity;
- SHA-256 fingerprint of the exact input snapshot;
- observed and expiry timestamps;
- allowlisted specialist role and agent identifier;
- explicit COMPLETE, INCOMPLETE or FAILED status;
- JSON-safe, bounded, immutable evidence payload;
- sanitized error type only on failure;
- orders_authorized=false.

The deterministic supervisor rejects malformed, stale, future, expired or mismatched input. It runs only registered specialist handlers, bounds the payload size to 64 KiB, rejects non-finite data, and fails closed on exceptions. Duplicate roles and message identifiers are rejected by the aggregate run contract.

## Roles
Supported research roles include market observer, technical analyst, regime analyst, liquidity analyst, volatility analyst, pattern analyst, multi-timeframe analyst, strategy researcher, opportunity evaluator, performance auditor, data integrity guardian, portfolio analyst and anomaly detector.

Risk Guardian is deliberately excluded from specialist output roles. It remains the independent paper-intent authorization authority and is not a peer agent.

## Authority boundary
Payloads cannot carry order commands, order payloads, risk-limit mutation fields, risk allocation, leverage, position size, quantity, notional, stop-loss/take-profit execution controls, margin or Emergency Stop controls. No handler receives credentials or exchange clients through this protocol. No specialist score is converted into order authorization.

The protocol has no persistence/message bus at present. It is a versioned in-process contract and supervisor only. A future distributed message bus requires separate authentication, replay protection, delivery semantics, audit storage and security review.

## PPO / RL / GAT boundary
Specialist evidence may become research input only. PPO/RL/GAT models remain offline research-only. Their training datasets must be built with the validated learning-data gate, exclude untouched holdout data, and pass independent chronological evaluation. A positive evaluation may only return REVIEW_REQUIRED; it never promotes a model automatically or changes Risk Guardian settings.

## Operational status language
- CONNECTED refers only to currently wired deterministic backend analysis or scheduler components.
- IMPLEMENTED refers to a module or handler contract available in source.
- PLANNED / NOT CONNECTED refers to autonomous agents, distributed message buses, PPO/RL training and GAT training that are not running.
Do not present the specialist role catalogue as 15 active autonomous AI agents.

## Safety invariants
- PAPER_ONLY and orders_authorized=false remain mandatory.
- Emergency Stop is never auto-cleared.
- No live orders or exchange order endpoints are accessible through the protocol.
- Stale, mismatched or incomplete evidence cannot be represented as a complete specialist run.
