# Continuous Opportunity Engine Design

**Date:** 2026-10-31  
**Project:** Solo AI Auto Trader  
**Status:** Approved design direction; implementation not started  
**Target branch:** `fix/futures-context-mainline`  
**Safety boundary:** paper-only / read-only; `orders_authorized=False`

## 1. Purpose and scope

Solo needs a continuous, restart-safe pipeline that discovers market candidates, assesses each candidate independently across eligible strategies, and maintains multiple independent opportunities while waiting for their own conditions. A trigger is only a request for fresh validation; it is never an order instruction.

This design extends the existing Market Watcher, multi-timeframe assessment, opportunity research and signal-watcher foundations. It does not introduce a competing market-data feed or a second risk authority.

### In scope

- Continuous scanning of the configured instrument universe using the existing shared market-data layer.
- Independent strategy assessments for scalping, day trading, swing trading, position trading and long-term analysis.
- Multiple concurrent opportunities, including multiple distinct setups for one symbol.
- Durable PostgreSQL state, idempotent events, worker claims and restart recovery.
- Fresh signal validation and independent Risk Manager review after a trigger.
- Auditability, observability, deterministic tests and staged integration.

### Out of scope

- Live trading or enabling any live exchange order endpoint.
- Forex or Spot activation before their own market-data adapters, session rules (where applicable), tests and verification exist.
- Treating Testnet prices, liquidity or fills as production evidence.
- Claiming profitability or calibrated win probabilities from heuristic scores.
- Letting AI, strategy agents, RL, GAT or generated configuration change risk limits or authorize orders.
- A hosting-provider migration or paid service decision.

## 2. Existing foundation and design constraints

The implementation must reuse these existing components:

| Existing component | Intended responsibility | Gap this design addresses |
|---|---|---|
| Shared Market Watcher / market-data worker | Instrument monitoring and validated shared snapshots | Connect scan results to durable opportunity coordination |
| `backend/market_assessment.py` | Deterministic multi-timeframe market context | Consume its validated assessments without duplicating analysis |
| `backend/opportunity_intelligence.py` | Research-only opportunity screening and strategy consensus | Preserve per-strategy decisions and persist assessment history |
| `backend/signal_watcher.py` | Deterministic trigger evaluation | Replace process-local watch state with durable orchestration |
| PostgreSQL job/lease infrastructure | Durable jobs and worker ownership | Reuse leases/idempotency patterns for opportunity processing |
| Independent Risk Manager / paper pipeline | Exclusive paper-intent authorization | Add a strictly validated proposal handoff after trigger |

The current signal watcher stores requests, statuses and notification flags in memory. It is a useful evaluation primitive but is not sufficient as the durable registry. The current opportunity intelligence function is stateless and its score is a screening heuristic, not a calibrated probability. These limitations must remain explicit in code and UI.

The currently configured Market Watcher source is Binance USDⓈ-M Futures Testnet. Spot and Forex must remain unavailable until separate adapters and their own verification are completed.

## 3. Architecture

The logical pipeline is:

1. Shared Market Data Layer
2. Continuous Market Search Engine
3. Multi-Strategy Assessment Engine
4. Multi-Opportunity Waiting Engine
5. Fresh Signal Validation
6. Independent Risk Manager
7. Paper Execution and Position Management
8. Audit and notification publication

The engines communicate through typed, versioned records and durable events. They do not share mutable in-memory authority.

### 3.1 Continuous Market Search Engine

**Purpose:** find and refresh candidates; never authorize a paper intent.

Responsibilities:

- Scan only configured and enabled instruments and supported market types.
- Use shared validated snapshots and closed candles; do not open a competing exchange feed.
- Apply data freshness, continuity, timestamp, history and market-quality checks before analysis.
- Evaluate configured timeframe groups and relevant market regimes.
- Detect candidate patterns such as continuation, pullback, reversal, breakout and failed breakout.
- Submit candidates with immutable evidence references and scan metadata to strategy assessment.
- Record scan start/end, coverage, skipped instruments, failures and source freshness.
- Respect bounded scan cadence, concurrency and per-cycle workload limits.

A scan with stale, incomplete or invalid source data must produce a visible data-quality failure or no-candidate result, not a fabricated opportunity.

### 3.2 Multi-Strategy Assessment Engine

Each strategy is an independent assessor with a versioned configuration and explicit evidence requirements.

| Strategy | Intended horizon | Authority |
|---|---|---|
| Scalping | Very short-term | May propose a paper candidate after validation |
| Day trading | Intraday | May propose a paper candidate after validation |
| Swing trading | Multi-day | May propose a paper candidate after validation |
| Position trading | Longer-term | May propose a paper candidate after validation |
| Long-term analysis | Weeks to years | Analysis and notification only; never paper-intent eligible |

Each strategy assessment records:

- Strategy identifier and version.
- Symbol, market type, direction and required timeframes.
- Assessment timestamp and source-data version/references.
- Decision: `ACCEPT_FOR_MONITORING`, `REJECT`, `REQUEST_MORE_EVIDENCE` or `NO_TRADE`.
- Evidence, conflicts, uncertainty and explicit rejection reasons.
- Proposed trigger, invalidation and expiry policy when monitoring is justified.
- Estimated fees, spread, slippage and funding costs where applicable.
- Calibration status and assessment provenance.

A strategy may request additional evidence, such as a closed candle, higher-timeframe confirmation, volume context or a fresh market snapshot. The coordinator must not silently convert missing evidence into approval.

Strategy disagreement is preserved. A rejection from one strategy does not cancel another strategy's distinct assessment. No simple majority vote across correlated timeframes or strategies may conceal disagreement.

Heuristic scores must be labelled `NOT_CALIBRATED` until reproducible walk-forward and untouched holdout evaluation supports calibration. A score is not a win probability or profit forecast.

### 3.3 Multi-Opportunity Waiting Engine

**Purpose:** maintain independent, durable opportunities until each reaches a terminal state or is deliberately superseded.

Responsibilities:

- Persist each eligible opportunity and its strategy-specific watch conditions.
- Support multiple symbols and multiple simultaneous setups for the same symbol.
- Reassess opportunities when relevant market data, evidence or scheduled review events arrive.
- Evaluate trigger, invalidation and expiry conditions against fresh validated data.
- Emit a single idempotent trigger event per valid opportunity transition.
- Recover active opportunities and pending work after process restart.
- Publish state changes and reasons to the API/app and audit stream.

A setup's identity must distinguish genuinely different strategies, timeframes, directions, trigger definitions and evidence versions. Deduplication must suppress repeated discoveries of the same setup without merging independent setups.

### 3.4 Fresh validation and Risk Manager boundary

A trigger transitions the opportunity into a validation workflow. It does not create an order or paper intent.

Before Risk Manager review, the pipeline must revalidate:

- Latest market snapshot and closed-candle freshness.
- Strategy conditions and invalidation state.
- Symbol/market eligibility and exchange constraints.
- Position and portfolio exposure across all strategies.
- Available simulated balance, sizing, stop distance and loss budget.
- Daily loss, allocation, leverage and Emergency Stop.
- Duplicate intent/idempotency key and current portfolio state.

The independent Risk Manager remains the sole authority for paper-intent acceptance. Failure, timeout, missing data, uncertain portfolio state or database error fails closed and records the reason. Long-term analysis is excluded from this handoff by construction, not merely by UI configuration.

Only an approved paper intent may enter the existing paper execution/position-management pipeline. Live order authorization remains disabled.

## 4. Opportunity lifecycle

Primary transitions:

`DISCOVERED → ASSESSING → WAITING → REASSESSING → TRIGGERED → RISK_CHECK → PAPER_OPEN`

Terminal or blocking outcomes:

- `REJECTED`: strategy or coordinator rejects the candidate.
- `INVALIDATED`: its invalidation condition is confirmed.
- `EXPIRED`: its strategy-specific expiry has elapsed.
- `CANCELLED`: explicitly cancelled or superseded under a recorded rule.
- `BLOCKED`: fresh validation or Risk Manager denies progression.
- `NO_TRADE`: assessment concludes that no eligible setup exists.

State transitions must be validated against an explicit transition table. Each transition records prior state, new state, reason, event ID, timestamp, actor/worker and evidence version. Replayed events must not repeat side effects.

A triggered opportunity may be returned to a non-terminal review state only through an explicit, audited transition; it must not be silently reset to WAITING.

## 5. Persistence model

Use the existing Supabase PostgreSQL database and migration conventions. Proposed logical records (final physical names and types are to be settled during implementation planning):

| Record | Required information |
|---|---|
| Opportunity | Stable ID, symbol, market type, strategy, timeframe, direction, state, version, created/updated/expiry timestamps |
| Strategy assessment | Strategy/version, decision, score/calibration label, evidence references, reasons, assessed timestamp |
| Evidence reference | Snapshot/candle identifiers, source, timeframe, observed time, data-quality status |
| Watch condition | Trigger rule/value, invalidation rule/value, expiry policy and last evaluated data version |
| Opportunity event | Append-only state transitions and idempotency/event keys |
| Worker claim | Owner, lease expiry, heartbeat and attempt metadata |
| Scheduled job run | Job key, due time, status, retry count and completion/error details |

Use foreign keys, constraints and indexes appropriate to actual query patterns. Store large evidence payloads by reference where possible; avoid copying entire candle histories into every opportunity row.

### Idempotency and identity

- Every scan run, assessment version, state transition and trigger event has a stable idempotency key.
- Unique constraints must enforce the intended deduplication boundary.
- A repeated event returns the previously recorded result rather than creating a second transition or paper intent.
- Different strategies or materially different setup definitions remain separate opportunities.
- Schema migration must be additive and reversible where practical; preserve existing ledger and market-intelligence data.

## 6. Concurrency, scheduling and recovery

- Claim work using PostgreSQL-backed leases or the established job-claim mechanism.
- Use atomic state/version checks when transitioning opportunities.
- Expired claims are reclaimable after lease expiry; heartbeat and stale-worker rules apply.
- Multiple workers may process different opportunities concurrently, but not the same transition.
- Scheduled scans and reassessments must be idempotent and must not block market-feed polling or heartbeats.
- Retry transient failures with bounded backoff; classify permanent validation failures.
- Database unavailability must stop durable processing rather than falling back to an in-memory registry.
- On startup, recover eligible active opportunities, expired claims and due scheduled work without replaying completed side effects.
- Configure maximum active opportunities, per-symbol active setups, worker concurrency, scan cadence and retention. Defaults must be conservative and validated under load before increasing.

## 7. Failure handling and observability

Expose and log:

- Last successful scan and scan coverage.
- Market-data source freshness and quality.
- Active opportunities by state, strategy, symbol and timeframe.
- Strategy assessment latency, errors and evidence requests.
- Oldest waiting opportunity and overdue reassessments.
- Worker heartbeat, lease age, retries and stuck jobs.
- Trigger-to-risk-check latency and blocked reasons.
- Duplicate suppression and idempotency conflicts.
- Database and event-publication failures.

If the market feed becomes stale, affected opportunities move to a visible waiting/recheck condition and cannot trigger a paper intent. If worker ownership or portfolio state is uncertain, processing fails closed. Last-known-good market values must never be presented as current.

## 8. API and Android behavior

The API should expose read-only views of scan health, opportunity lists, per-opportunity history, strategy assessments and reasons. Mutating operations must be authenticated and limited to explicit user actions such as cancellation or configuration changes.

The Android experience should show:

- Search/worker status and last successful scan.
- Active opportunities with symbol, strategy, timeframe, direction and state.
- Trigger and invalidation conditions, expiry and latest evidence time.
- Strategy agreement and disagreement, with each strategy's own rationale.
- Data-quality warnings and no-trade/blocked reasons.
- A clear distinction between `TRIGGERED`, `RISK_CHECK` and `PAPER_OPEN`.
- Long-term analysis as advisory-only.
- Emergency Stop and risk status without suggesting AI can override them.

Do not show uncalibrated heuristic scores as percentages of success.

## 9. Security and safety invariants

These invariants apply to every engine, event consumer, API route and test:

1. `orders_authorized=False` remains enforced throughout development and staging.
2. No live exchange order endpoint is enabled or called.
3. Only the independent Risk Manager can authorize paper intents.
4. Strategy engines and AI agents cannot call the paper order endpoint directly.
5. Long-term analysis cannot create paper intents.
6. Generated strategy settings cannot raise risk limits or disable controls.
7. Emergency Stop blocks new paper intents.
8. Stale data, invalid state, failed risk checks, uncertain portfolio state and database errors fail closed.
9. Secrets remain in encrypted server-side configuration and never enter the APK, prompts or logs.
10. Every candidate, assessment, transition, risk decision and paper execution result is auditable.

## 10. Verification and acceptance criteria

### Unit and contract tests

- Each strategy can accept, reject, request evidence and return no-trade independently.
- Strategy disagreement remains visible and does not cross-cancel independent opportunities.
- Same-symbol, same-strategy and same-timeframe distinct setups coexist when their setup identities differ.
- Duplicate discoveries are deduplicated only within the defined identity boundary.
- Trigger, invalidation, expiry and cancellation transitions obey the state machine.
- Stale, future-dated, incomplete and invalid market data cannot trigger a valid proposal.
- Long-term analysis is structurally excluded from paper-intent creation.
- Risk Manager rejection and Emergency Stop block paper intents.
- Idempotent replay creates no duplicate transitions or intents.

### PostgreSQL and concurrency tests

- Concurrent workers cannot claim/process the same transition twice.
- Lease expiry and heartbeat recovery work after worker termination.
- API and worker restarts recover active opportunities and due jobs.
- Database outages fail closed; no ephemeral fallback is reported as durable.
- Migrations preserve existing ledger and market-intelligence records.
- Transaction rollback leaves no partial state transition or duplicate event.

### Integration and staging acceptance

- Continuous scan publishes candidates from the shared market-data layer.
- Multiple strategies and opportunities are visible through authenticated API contracts.
- Trigger events invoke fresh validation and independent Risk Manager review only.
- Approved paper intents reconcile with the existing paper ledger and position manager.
- Restart, stale-feed, duplicate-event, database-recovery and Emergency Stop drills pass.
- Android shows current state, timestamps, rationale and data-quality status correctly.

CI success alone does not establish staging operation, device acceptance or profitability. Each status must be reported separately.

## 11. Delivery sequence and dependencies

1. Review and freeze this design.
2. Inspect existing PostgreSQL schema, migration patterns, job leases and paper Risk Manager interfaces.
3. Produce the implementation plan and explicit API/event contracts.
4. Add additive opportunity/assessment/watch/event persistence.
5. Implement state transitions and idempotent worker claims.
6. Connect the shared Market Watcher scan output to independent strategy assessments.
7. Add durable waiting, reassessment, expiry and recovery.
8. Add trigger-to-fresh-validation-to-Risk-Manager handoff.
9. Integrate approved paper intents with existing paper execution and position management.
10. Add authenticated read APIs and Android opportunity views.
11. Complete automated tests, staging recovery drills and phone acceptance.

The continuous worker cannot be declared operational until an always-on runtime is available and verified. The current Render Free staging service is not a suitable always-on runtime. This design does not authorize a provider migration, payment method, paid plan or any charge.

## 12. Open implementation-planning decisions

These are intentionally deferred to implementation planning because they require inspection of existing contracts and workload evidence:

- Exact PostgreSQL table/column names and migration boundaries.
- Exact opportunity identity key and which setup fields define material difference.
- Per-strategy default expiry and reassessment cadence.
- Active-opportunity, per-symbol and worker-concurrency limits.
- Exact API routes, event names and Android pagination/filter behavior.
- Whether event publication uses an existing durable outbox or needs a small additive outbox.

These decisions must be resolved before coding the affected interfaces. They do not weaken the safety invariants above.

## 13. Explicit current limitations

At the time of this design:

- The current configured market source is Binance USDⓈ-M Futures Testnet only.
- Spot and Forex adapters are not established by this work.
- Existing opportunity assessment is stateless; existing signal watcher is in-memory.
- The Market Watcher candidate stream is not yet connected to the Risk Manager or paper execution.
- Strategy performance is not calibrated and profitability is unproven.
- Staging continuous-worker operation is blocked by the current always-on hosting limitation.
- No live trading capability is authorized or claimed.
