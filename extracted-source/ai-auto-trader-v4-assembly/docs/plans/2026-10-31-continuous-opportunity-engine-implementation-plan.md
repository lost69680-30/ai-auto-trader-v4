# Continuous Opportunity Engine Implementation Plan

> **For agentic workers:** Use the host's available task-by-task implementation workflow. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a restart-safe, paper-only pipeline that continuously searches the configured Testnet market universe, independently assesses five strategy horizons, and maintains multiple durable opportunities without bypassing fresh validation or the independent Risk Manager.

**Architecture:** Reuse the existing shared market-data worker, validated PostgreSQL snapshots/candle archive, market assessment, research functions, durable scheduler, signal validation, and paper orchestration. Add a PostgreSQL opportunity registry and coordinator that owns state transitions; strategy assessors produce research-only records; a trigger only schedules fresh validation and Risk Manager review. Keep the API read-only for opportunity data and keep live exchange order capability absent.

**Tech Stack:** Python 3.11+, pytest, psycopg 3, PostgreSQL/Supabase, existing Binance USDⓈ-M Futures Testnet adapter, existing GitHub Actions CI.

## Global Constraints

- Paper-only/read-only throughout implementation and testing. Every new record/result must carry or enforce `orders_authorized = false`.
- Never add a live order endpoint, live exchange execution client, credential path, or AI-controlled risk-setting mutation.
- The independent Risk Manager remains the only authority allowed to accept a paper intent. Strategies, search, opportunity coordinator, triggers, RL/GAT and generated configuration cannot authorize paper intents.
- A trigger is only a request for fresh signal/data validation. It is not an order or paper intent.
- Long-term analysis is analysis/notification only and must be structurally ineligible for signal handoff and paper-intent creation.
- Reuse the existing shared validated market-data layer. Do not add a competing exchange feed or duplicate market polling.
- The active source remains Binance USDⓈ-M Futures Testnet. Do not claim Spot or Forex support; do not activate them in this work.
- Use closed candles and enforce freshness, timestamp, continuity, source, venue and market-type checks. Stale, missing, malformed or uncertain data fails closed.
- Preserve independent strategy disagreements and their reasons. Do not use a simple majority vote to erase disagreement or allow one strategy's rejection to cancel another independent setup.
- Heuristic scores remain `NOT_CALIBRATED`; they are not probabilities, expected returns or profitability claims.
- Support multiple active opportunities for different symbols and multiple distinct opportunities for the same symbol.
- Persist state and event history in the existing Supabase PostgreSQL database. Preserve existing paper ledger, market feed, candle archive, scheduled jobs and intelligence reports.
- Use idempotent writes, atomic state/version transitions, bounded concurrency, worker leases/heartbeats and restart recovery.
- API exposure is authenticated and read-only for opportunity state. No new API mutation for order/position/risk state.
- Do not change Render, hosting, GitHub deployment settings, secrets, database provider, billing or service plans. Do not deploy or incur charges.
- Keep the implementation on `fix/futures-context-mainline`; do not merge to `main` as part of this plan.

---

## Repository baseline and observed integration points

The plan is grounded in the assembly tree on `fix/futures-context-mainline`.

- `backend/market_assessment.py`: deterministic multi-timeframe context and closed-candle quality checks.
- `backend/opportunity_intelligence.py`: stateless research-only opportunity assessment; its score is heuristic.
- `backend/strategy_research.py`: deterministic strategy research primitives; its current assessment labels are not yet the five requested trading horizons.
- `backend/market_intelligence_scan.py`: existing scheduled scan pattern that reuses the market source, fetches supported intervals concurrently, and publishes compact research reports.
- `backend/shared_market_store.py`: PostgreSQL shared feed store and worker lease/heartbeat pattern.
- `backend/shared_market_worker.py`: current worker entry point and durable scheduled-job dispatch loop.
- `backend/durable_scheduler.py` and `backend/analysis_job_dispatcher.py`: durable idempotent scheduled work and allowlisted read-only handlers.
- `backend/signal_watcher.py`: deterministic trigger evaluation primitive; its state is explicitly in-memory and must not become the durable registry.
- `app/engines/signal_handoff.py`: explicit price-plan-to-signal validation boundary.
- `app/engines/paper_signal_cycle.py` and `app/engines/orchestrator.py`: trigger, fresh validation and independent risk-to-paper-ledger path.
- `app/engines/read_only_api.py`: authenticated API and PostgreSQL ledger selection.
- Existing root migrations: `supabase/migrations/20261030195452_create_shared_market_data.sql`, `20261030195505_create_durable_scheduled_jobs.sql`, and `20261031155011_create_market_intelligence_reports.sql`.
- Existing focused tests include `tests/test_market_intelligence_scan.py`, `tests/test_market_assessment.py`, `tests/test_shared_market_data_store.py`, `tests/test_analysis_job_dispatcher.py`, and `backend/tests/test_signal_watcher.py`.

All new paths below are proposed; preserve the assembly's existing package and test layout.

## Task 1: Add the durable opportunity registry and append-only event store

**Files:**
- Create: `supabase/migrations/20261031170000_create_opportunity_registry.sql` (proposed; proposed timestamp; confirm no later migration exists before applying).
- Create: `extracted-source/ai-auto-trader-v4-assembly/backend/opportunity_store.py` (proposed).
- Test: `extracted-source/ai-auto-trader-v4-assembly/tests/test_opportunity_store.py` (proposed).
- Modify: migration test setup in `extracted-source/ai-auto-trader-v4-assembly/tests/test_shared_market_data_store.py` only to include the new migration in the test database reset sequence, if that test harness remains the shared migration runner.

**Interfaces:**
- Produce immutable records: `OpportunityRecord`, `StrategyAssessmentRecord`, `OpportunityEvidenceRecord`, `WatchConditionRecord`, and `OpportunityEventRecord`.
- Produce `PostgresOpportunityStore(dsn)` with methods:
  - `create_or_get_opportunity(record, *, idempotency_key, now) -> OpportunityRecord`
  - `append_assessment(record, *, idempotency_key, now) -> StrategyAssessmentRecord`
  - `save_watch_condition(record, *, idempotency_key, now) -> WatchConditionRecord`
  - `get_opportunity(opportunity_id) -> OpportunityRecord | None`
  - `list_opportunities(*, states, symbol, limit, cursor) -> OpportunityPage`
  - `claim_due_opportunities(*, owner, now, limit, lease_seconds) -> tuple[OpportunityRecord, ...]`
  - `transition(opportunity_id, *, expected_version, to_state, reason, event_id, actor, evidence_version, now) -> TransitionResult`
  - `release_claim(opportunity_id, *, owner, now) -> bool`
- Consume the existing PostgreSQL DSN and SSL helper pattern from `backend/shared_market_store.py`; no new database provider or connection secret.

- [x] **Step 1: Add focused failing repository and migration tests**

Test schema constraints and repository behavior using the existing `PAPER_TEST_DATABASE_URL` convention:
1. A new opportunity and its initial event persist and are readable after a new store instance is created.
2. Two records with the same idempotency key return the same logical result and do not append duplicate events.
3. A repeated symbol with a different strategy/setup fingerprint remains a separate opportunity.
4. An invalid transition is rejected; a valid transition increments the version exactly once and appends one event.
5. A stale expected version cannot overwrite a newer state.
6. Concurrent claims by two owners cannot claim the same active opportunity; an expired claim can be reclaimed.
7. Database failure, missing migration, malformed JSON, or `orders_authorized=true` fails closed.
8. Existing ledger, market feed, candle archive, scheduled-job and intelligence-report tables remain intact after migration.

- [x] **Step 2: Verify the expected failures**

Run from `extracted-source/ai-auto-trader-v4-assembly`:

```bash
pytest -q tests/test_opportunity_store.py
```

Expected: the focused tests fail because the migration, records and repository do not yet exist.

- [x] **Step 3: Implement additive PostgreSQL schema and repository**

Create separate relational records for opportunities, strategy assessments, evidence references, watch conditions, append-only opportunity events and opportunity claims. Use foreign keys and indexes for active-state lookup, symbol/timeframe lookup, expiry processing, event history and idempotency lookup. Keep compact evidence references to existing snapshot/candle/archive identifiers; do not copy full candle histories into each opportunity.

Enforce `orders_authorized = false` with database CHECK constraints on all opportunity-owned tables that expose that field. Enable RLS and revoke direct access from `PUBLIC`, `anon`, `authenticated` and `service_role` following the existing private backend-schema migration pattern. Do not add credentials or a public Data API path.

Implement transitions in a database transaction using row locking or a compare-and-swap version predicate. The event append and state update must commit or roll back together. A duplicate event key returns the recorded outcome without repeating side effects. A claim lease is owner-bound and reclaimable only after expiry. Never treat a lost claim or database exception as permission to continue processing.

The state transition validator must encode the design's allowed lifecycle and terminal states. It must reject direct transitions to `PAPER_OPEN` from search, assessment, waiting or trigger code. The later risk-approved integration must use a distinct recorded-intent state/event if the paper ledger only accepts an intent; it may transition to `PAPER_OPEN` only if the existing paper ledger confirms the simulated position is actually open. Final state mapping is gated by product decision 6 below.

- [x] **Step 4: Verify the focused pass**

Run:

```bash
pytest -q tests/test_opportunity_store.py
```

Expected: all repository and migration tests pass against a migrated test PostgreSQL database; tests requiring an unavailable database are explicitly skipped with the existing documented reason, not reported as passed.

- [x] **Step 5: Run affected integration checks**

Run:

```bash
pytest -q tests/test_shared_market_data_store.py tests/test_analysis_job_dispatcher.py
```

Expected: existing shared market-data and scheduled-job behavior remains green; the new migration does not remove or weaken existing tables, leases or safety constraints.

- [x] **Step 6: Commit the passing deliverable**

```bash
git add supabase/migrations/20261031170000_create_opportunity_registry.sql extracted-source/ai-auto-trader-v4-assembly/backend/opportunity_store.py extracted-source/ai-auto-trader-v4-assembly/tests/test_opportunity_store.py extracted-source/ai-auto-trader-v4-assembly/tests/test_shared_market_data_store.py
git commit -m "feat: add durable opportunity registry"
```

## Task 2: Build independent, versioned strategy assessments

**Files:**
- Create: `extracted-source/ai-auto-trader-v4-assembly/backend/strategy_assessment_engine.py` (proposed).
- Create: `extracted-source/ai-auto-trader-v4-assembly/backend/strategy_profiles.py` (proposed; profile definitions only).
- Test: `extracted-source/ai-auto-trader-v4-assembly/tests/test_strategy_assessment_engine.py` (proposed).
- Reuse without rewriting: `backend/strategy_research.py`, `backend/market_assessment.py`, and `backend/opportunity_intelligence.py`.

**Interfaces:**
- `StrategyAssessor` protocol: `assess(candidate, evidence, *, now) -> StrategyAssessmentRecord`.
- `StrategyProfile`: strategy ID/version, required timeframes, evidence requirements, eligibility class and assessor reference.
- `MultiStrategyAssessmentEngine(profiles, assessors).assess(candidate, evidence, *, now) -> tuple[StrategyAssessmentRecord, ...]`.
- `assessors` is an explicit registry keyed by strategy ID; a missing or failed assessor yields a recorded `REQUEST_MORE_EVIDENCE` or `NO_TRADE` result, never an implicit approval.

- [x] **Step 1: Add failing tests for strategy independence and authority boundaries**

Cover:
1. Each of the five strategy IDs receives its own assessment record and version.
2. A rejection from scalping does not erase a swing assessment for the same symbol.
3. Different directions remain visible as disagreement; no majority vote silently converts them into agreement.
4. Missing required timeframe evidence yields `REQUEST_MORE_EVIDENCE`; invalid/stale evidence yields a non-eligible result.
5. Long-term analysis is labelled analysis-only and cannot produce an eligible watch condition or signal-handoff input.
6. One assessor exception does not discard other completed assessments; the failed assessor's failure is recorded with bounded safe diagnostics.
7. Scores include `NOT_CALIBRATED` until an independently verified calibration artifact is supplied.
8. Every assessment explicitly has `orders_authorized=false`.

- [x] **Step 2: Verify the expected failures**

Run:

```bash
pytest -q tests/test_strategy_assessment_engine.py
```

Expected: collection fails until the new strategy profile and assessment interfaces are defined.

- [x] **Step 3: Implement the assessment registry and five isolated profiles**

Implement the five explicit strategy identities: scalping, day trading, swing trading, position trading and long-term analysis. Reuse existing market assessment and research outputs as evidence; do not relabel a single existing consensus score as five independent assessments. Each profile must evaluate its own configured evidence/timeframes and emit its own decision, reasons, uncertainty, version, evidence references, costs where available, trigger/invalidation proposal where justified, expiry policy reference and calibration label.

Keep the assessment engine pure with respect to persistence, exchange access, account state, risk settings and paper ledger. Persist results only through Task 1's repository. Keep strategy configuration versioned and immutable per assessment so a later configuration change cannot rewrite historical decisions.

Do not implement a profitability model or claim statistical calibration. Do not permit the long-term profile to create a watch condition eligible for the paper path.

- [x] **Step 4: Verify the focused pass**

Run:

```bash
pytest -q tests/test_strategy_assessment_engine.py backend/tests/test_opportunity_intelligence.py tests/test_market_assessment.py
```

Expected: new per-strategy tests pass and existing research/market-assessment behavior remains unchanged.

- [x] **Step 5: Run affected integration checks**

Run:

```bash
pytest -q tests/test_market_intelligence_scan.py tests/test_complete_analysis_pipeline.py
```

Expected: existing scan and research-to-signal boundaries remain intact; the new strategy engine does not create signals or paper intents.

- [x] **Step 6: Commit the passing deliverable**

```bash
git add extracted-source/ai-auto-trader-v4-assembly/backend/strategy_assessment_engine.py extracted-source/ai-auto-trader-v4-assembly/backend/strategy_profiles.py extracted-source/ai-auto-trader-v4-assembly/tests/test_strategy_assessment_engine.py
git commit -m "feat: add independent strategy assessments"
```

## Task 3: Add continuous market search on the existing shared feed

**Files:**
- Create: `extracted-source/ai-auto-trader-v4-assembly/backend/continuous_opportunity_search.py` (proposed).
- Test: `extracted-source/ai-auto-trader-v4-assembly/tests/test_continuous_opportunity_search.py` (proposed).
- Modify: `extracted-source/ai-auto-trader-v4-assembly/backend/market_intelligence_scan.py` only where a small reusable fetch/quality helper is required; preserve its existing report contract.
- Reuse: `backend/shared_market_store.py`, `backend/shared_market_worker.py`, `backend/market_intelligence_scan.py`, `backend/market_assessment.py`, and the existing market adapter/candle archive.

**Interfaces:**
- `SearchConfig`: configured universe reference, enabled market types, scan timeframe set, candle lookback, bounded fetch concurrency, snapshot age limit and scan version.
- `ContinuousOpportunitySearch(store, source, strategy_engine, config).scan(*, now, scan_id) -> SearchRunResult`.
- `SearchRunResult`: scan ID, started/completed timestamps, coverage counts, per-symbol quality status, candidate IDs, skipped symbols/reasons and `orders_authorized=false`.

- [x] **Step 1: Add failing tests for scan coverage and quality gates**

Cover:
1. Only configured and enabled Testnet instruments are scanned.
2. Each requested timeframe is fetched from the existing source and only closed candles are used for confirmed evidence.
3. Duplicate, missing, stale, future-dated, gapped, wrong-symbol or malformed evidence prevents candidate creation for the affected symbol/timeframe.
4. One symbol's fetch failure does not silently erase valid results for other symbols; the run records partial coverage and explicit errors.
5. A scan with no valid candidates is a successful `NO_CANDIDATES` run, not a fabricated opportunity.
6. Fetch concurrency is bounded by configuration and no duplicate exchange feed/client is constructed.
7. A repeated scan ID/idempotency key does not create duplicate opportunities or assessments.
8. All results remain research-only and have `orders_authorized=false`.

- [x] **Step 2: Verify the expected failures**

Run:

```bash
pytest -q tests/test_continuous_opportunity_search.py
```

Expected: the new scan interface is absent and focused tests fail at import/collection.

- [x] **Step 3: Implement the search handler**

Build a bounded scan over the configured allowlist using the worker's existing public Testnet source and the same market-data quality rules used by the shared feed and market assessment. Reuse the existing concurrent timeframe-fetch pattern rather than creating another polling loop. Record run coverage and quality outcomes even when a symbol is skipped. Pass immutable evidence references to Task 2 and persist the five independent strategy assessments inside the existing durable market-intelligence report. Do not create registry opportunities in this task: the exact setup identity must include an explicit strategy-approved trigger/price plan, which is established at the Task 4 coordinator boundary. This avoids creating duplicate or misleading waiting records from a scan-only heuristic.

A scan must not synthesize entry/stop/target prices or create signals. It must not assume Spot or Forex is configured. Repeated scan delivery must be safe under at-least-once scheduled-job execution.

- [x] **Step 4: Verify the focused pass**

Run:

```bash
pytest -q tests/test_continuous_opportunity_search.py
```

Expected: configured-universe, quality-gate, partial-failure, bounded-concurrency and idempotency tests pass.

- [x] **Step 5: Run affected integration checks**

Run:

```bash
pytest -q tests/test_market_intelligence_scan.py tests/test_shared_market_data_store.py
```

Expected: the existing scheduled intelligence scan and shared snapshot store remain green with no competing market feed.

- [x] **Step 6: Commit the passing deliverable**

```bash
git add extracted-source/ai-auto-trader-v4-assembly/backend/continuous_opportunity_search.py extracted-source/ai-auto-trader-v4-assembly/tests/test_continuous_opportunity_search.py extracted-source/ai-auto-trader-v4-assembly/backend/market_intelligence_scan.py
git commit -m "feat: add continuous opportunity search"
```

## Task 4: Implement the durable multi-opportunity waiting coordinator

**Files:**
- Create: `extracted-source/ai-auto-trader-v4-assembly/backend/opportunity_coordinator.py` (proposed).
- Test: `extracted-source/ai-auto-trader-v4-assembly/tests/test_opportunity_coordinator.py` (proposed).
- Reuse: Task 1's `PostgresOpportunityStore` and the deterministic trigger comparisons in `backend/signal_watcher.py`.

**Interfaces:**
- `OpportunityCoordinator(store, trigger_evaluator, config)`.
- `register_assessed_opportunity(assessment, *, now) -> OpportunityRecord`.
- `reassess_due(*, now, limit) -> CoordinatorRunResult`.
- `evaluate_market_update(symbol, market_update, *, now) -> tuple[TransitionResult, ...]`.
- `cancel(opportunity_id, *, reason, actor, now) -> TransitionResult`.
- `triggered_opportunity(opportunity_id) -> OpportunityRecord | None` for the later fresh-validation consumer.

- [ ] **Step 1: Add failing lifecycle, concurrency and recovery tests**

Cover:
1. Multiple symbols can wait simultaneously.
2. BTCUSDT can hold separate scalping and swing opportunities, or two distinct setups of one strategy, without overwrite.
3. One market update evaluates every active watch for that symbol and only the matching opportunity transitions.
4. Trigger, invalidation and expiry are mutually consistent and produce exactly one terminal transition/event.
5. A stale market update produces `WAIT_RECHECK` and leaves the opportunity waiting; it never triggers.
6. Two workers racing the same opportunity cannot both commit the same transition.
7. A worker crash after claim but before completion is recovered after lease expiry.
8. Replayed trigger events do not duplicate validation requests or paper intents.
9. Strategy disagreement is preserved; rejecting one setup does not cancel a different setup.
10. Long-term analysis may be reassessed and surfaced but never enters the eligible waiting-to-paper path.
11. Illegal state transitions, missing evidence and uncertain database/portfolio state fail closed with an audit reason.

- [ ] **Step 2: Verify the expected failures**

Run:

```bash
pytest -q tests/test_opportunity_coordinator.py
```

Expected: tests fail because the durable coordinator and state transition flow are not yet implemented.

- [ ] **Step 3: Implement the lifecycle coordinator**

Use the Task 1 store for all durable state. Treat `backend.signal_watcher.py` as a pure comparison/validation primitive only; do not depend on its in-memory request/status dictionaries for restart recovery. Evaluate durable watch conditions against the latest validated shared snapshot/closed candle, then atomically transition with expected version and event idempotency key.

Support independent watch identity per setup. Keep trigger, invalidation, expiry, rejection, cancellation and blocked outcomes auditable. A trigger enqueues a distinct fresh-validation work item; it must not call the paper ledger or mark the opportunity paper-open. Reassessment must be bounded, lease-owned and safe to retry after worker restart.

- [ ] **Step 4: Verify the focused pass**

Run:

```bash
pytest -q tests/test_opportunity_coordinator.py
```

Expected: lifecycle, same-symbol parallel watches, idempotency, concurrent claims, expiry and recovery tests pass against fake clocks and the test PostgreSQL store.

- [ ] **Step 5: Run affected integration checks**

Run:

```bash
pytest -q backend/tests/test_signal_watcher.py tests/test_opportunity_store.py
```

Expected: deterministic watcher behavior remains unchanged and coordinator persistence/concurrency remains green.

- [ ] **Step 6: Commit the passing deliverable**

```bash
git add extracted-source/ai-auto-trader-v4-assembly/backend/opportunity_coordinator.py extracted-source/ai-auto-trader-v4-assembly/tests/test_opportunity_coordinator.py
git commit -m "feat: add durable multi-opportunity waiting"
```

## Task 5: Integrate scheduled worker, fresh validation, Risk Manager and read-only API

**Files:**
- Create: `extracted-source/ai-auto-trader-v4-assembly/backend/opportunity_worker.py` (proposed; read-only scheduled handlers only).
- Modify: `extracted-source/ai-auto-trader-v4-assembly/backend/shared_market_worker.py` to register and dispatch the new allowlisted opportunity scan/reassessment/validation jobs.
- Modify: `extracted-source/ai-auto-trader-v4-assembly/app/engines/read_only_api.py` to expose authenticated read-only opportunity list/detail/event endpoints.
- Modify: `extracted-source/ai-auto-trader-v4-assembly/app/engines/paper_signal_cycle.py` only at the validated trigger-to-existing-orchestrator boundary; do not weaken current checks.
- Test: `extracted-source/ai-auto-trader-v4-assembly/tests/test_opportunity_worker.py` (proposed).
- Test: `extracted-source/ai-auto-trader-v4-assembly/tests/test_opportunity_api.py` (proposed).
- Test: focused additions to `tests/test_paper_signal_cycle.py` and `tests/test_shared_market_supervisor.py`.

**Interfaces:**
- Register explicit read-only handlers for `continuous_opportunity_scan`, `opportunity_reassessment` and `opportunity_trigger_validation` through the existing `DurableJobScheduler` and `AnalysisJobDispatcher`.
- API: authenticated `GET /api/v1/opportunities`, `GET /api/v1/opportunities/{opportunity_id}`, and `GET /api/v1/opportunities/{opportunity_id}/events`. Response payloads are read-only, paginated and include freshness, strategy decisions, state/reason and `orders_authorized=false`.
- Trigger consumer: fetch current evidence, rerun signal validation and explicit price planning, then pass only a valid handoff to the existing paper cycle/orchestrator. The existing Risk Manager and durable paper ledger remain authoritative.

- [ ] **Step 1: Add failing worker/API/safety integration tests**

Cover:
1. Scheduler registration uses stable job names and idempotency keys; repeated schedule delivery does not duplicate scans or state transitions.
2. Worker dispatcher rejects unknown, execution-like or `orders_authorized=true` jobs.
3. Worker heartbeat and market-feed polling continue while opportunity scans/reassessments run; long work stays off the market-feed polling/heartbeat thread.
4. API endpoints require existing authentication, expose only persisted records, paginate deterministically and cannot mutate opportunities or trading state.
5. API omits secrets and internal database details.
6. Trigger consumer always obtains fresh validated market evidence and re-runs signal validation; stale or invalid data blocks progression.
7. Long-term strategy records are rejected by the trigger consumer even if a malformed record claims paper eligibility.
8. Risk Manager veto, Emergency Stop, daily-loss limit, allocation cap, exposure cap, missing simulated equity, DB failure and uncertain portfolio state prevent paper-intent recording.
9. Only a risk-approved paper intent recorded by the existing ledger can move an opportunity to `PAPER_OPEN`; duplicate trigger/job delivery creates at most one intent.
10. No code path sets live order authorization true or calls a live exchange order API.

- [ ] **Step 2: Verify the expected failures**

Run:

```bash
pytest -q tests/test_opportunity_worker.py tests/test_opportunity_api.py tests/test_paper_signal_cycle.py
```

Expected: new worker/API tests fail until the handlers and routes exist; existing paper-cycle tests remain baseline controls.

- [ ] **Step 3: Implement worker and API integration**

Register new scheduled jobs through the existing durable scheduler and allowlist only research, reassessment and validation handlers. Keep scan/reassessment execution independent of feed polling and worker heartbeat. Use existing worker lease and restart supervision; do not add another supervisor or market-data connection.

For a triggered opportunity, enqueue fresh validation. The validation handler must reload the current closed-candle/snapshot evidence, confirm the opportunity is still active and unexpired, create or retrieve an explicit strategy-approved price plan, call the existing `prepare_signal` validation boundary, and then pass the valid signal through the existing `PaperSignalCycle`/orchestrator. If a price plan cannot be produced from explicit evidence, record `BLOCKED` or `NO_TRADE`; never infer entry/stop/target from a heuristic score. The independent Risk Manager must approve before the existing ledger records a paper intent. Persist the intent-recorded event separately from a position-open event; use the product-approved state mapping to decide when `PAPER_OPEN` is valid. Long-term analysis is filtered by strategy eligibility in both coordinator and handoff boundary.

The API is read-only for opportunity records and event history. It must not expose database DSNs, worker owner secrets, exchange credentials or unbounded evidence blobs. Keep pagination bounded and use stable ordering.

- [ ] **Step 4: Verify the focused pass**

Run:

```bash
pytest -q tests/test_opportunity_worker.py tests/test_opportunity_api.py tests/test_paper_signal_cycle.py tests/test_shared_market_supervisor.py
```

Expected: worker scheduling, read-only API, fresh validation, Risk Manager veto and restart boundaries pass; no live-order interface is present.

- [ ] **Step 5: Run the full backend test suite and static checks**

Run from `extracted-source/ai-auto-trader-v4-assembly`:

```bash
python -m compileall -q app backend
pytest -q
```

Expected: compilation succeeds and all runnable tests pass. PostgreSQL integration tests may be skipped only when `PAPER_TEST_DATABASE_URL` is not configured; report skipped tests separately and do not describe them as verified. Then run the repository's existing GitHub Actions backend and Android CI workflows; no deployment is implied by CI success.

- [ ] **Step 6: Commit the passing deliverable**

```bash
git add extracted-source/ai-auto-trader-v4-assembly/backend/opportunity_worker.py extracted-source/ai-auto-trader-v4-assembly/backend/shared_market_worker.py extracted-source/ai-auto-trader-v4-assembly/app/engines/read_only_api.py extracted-source/ai-auto-trader-v4-assembly/app/engines/paper_signal_cycle.py extracted-source/ai-auto-trader-v4-assembly/tests/test_opportunity_worker.py extracted-source/ai-auto-trader-v4-assembly/tests/test_opportunity_api.py extracted-source/ai-auto-trader-v4-assembly/tests/test_paper_signal_cycle.py extracted-source/ai-auto-trader-v4-assembly/tests/test_shared_market_supervisor.py
git commit -m "feat: integrate durable opportunity workflow"
```

---

## Unresolved product and operating decisions

These are intentionally not silently fixed by the implementation plan. Engineering can implement configurable seams, but the corresponding defaults must be approved before enabling the relevant behavior:

1. **Strategy timeframe profiles:** confirm the exact candle/timeframe sets for scalping, day, swing, position and long-term analysis. Suggested starting point for review only: scalping `1m/5m`; day `5m/15m/1h`; swing `1h/4h/1d`; position `1d/1w/1M`; long-term `1w/1M/1Y`.
2. **Per-strategy expiry and reassessment cadence:** choose how long each opportunity remains valid and how often it is reassessed. Suggested defaults must be derived from strategy horizon and data cadence, not one global timeout.
3. **Opportunity identity/deduplication:** approve the fingerprint boundary. Recommended inputs are market type, symbol, strategy ID/version, direction, timeframe set, trigger definition and evidence/config version; two materially different trigger definitions must never collapse into one record.
4. **Universe and workload limits:** confirm the maximum enabled instruments, timeframe lookback, scan interval, per-cycle fetch concurrency, active-opportunity cap and reassessment batch size. Existing scan components currently constrain configured policies to 1–20 symbols and fetch concurrency to at most 20; expanding those limits needs separate capacity evidence.
5. **Price-plan source and eligibility:** confirm which existing price-planning method is permitted to create explicit entry/stop/target values for each strategy. If no valid plan exists, the correct outcome is no trade—not a guessed price.
6. **Paper position lifecycle mapping:** confirm whether `PAPER_OPEN` means an accepted paper intent or a ledger-confirmed open position. Until this decision is approved, the implementation must persist separate intent-recorded and position-open events and must not label an intent as a filled/open position.
7. **Notification behavior:** confirm whether users should be notified on every state transition or only on meaningful events such as new waiting opportunity, trigger, invalidation, expiry and Risk Manager block.
8. **Activation gate:** continuous scanning cannot be claimed operational until an always-on worker host and its recovery behavior are verified. The current Render Free API-only service does not satisfy this requirement. This plan does not authorize a hosting change or deployment.

## Completion criteria

The feature is not complete when code merely exists. Completion requires:
- All focused unit and PostgreSQL integration tests pass (or clearly reported database-dependent skips).
- CI passes for the exact integration-branch commit.
- Migration apply/rollback behavior is verified without disturbing existing paper ledger or market-data tables.
- Worker/API independent restart, lease expiry, duplicate job delivery and opportunity recovery are verified against PostgreSQL.
- Trigger-to-fresh-validation-to-Risk-Manager-to-paper-ledger behavior is demonstrated with paper-only test data, including all fail-closed cases.
- Authenticated API responses are verified read-only.
- Staging deployment and Android phone acceptance are separate later gates and require explicit authorization; neither is part of this plan's execution.
