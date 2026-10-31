# Solo Market Watcher and Adaptive Analysis Implementation Plan

> **For agentic workers:** Use the host's available task-by-task implementation workflow. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect Solo's existing validated market feed, durable worker state, multi-timeframe analysis, and adaptive strategy selection into a traceable paper-only autonomous workflow.

**Architecture:** Extend the existing worker/shared PostgreSQL snapshot/WebSocket/API components instead of creating duplicate market feeds. Add a deterministic multi-timeframe assessment contract and adaptive strategy selector that produce research proposals only; the existing independent risk guardian remains the sole paper-intent gate.

**Tech Stack:** Python 3.11+, pytest, PostgreSQL via psycopg, existing WSGI/ASGI API, Kotlin/Jetpack Compose Android.

## Global Constraints

- All source and runtime outputs must keep `orders_authorized=False`.
- No exchange order-placement/cancellation adapter, private account execution, live trading or credential handling may be introduced.
- The Market Watcher can scan and rank opportunities but cannot authorize or create orders.
- Strategy proposals must pass through the existing independent Risk Guardian and paper orchestration path.
- Missing, stale, malformed, incomplete, future-dated or contradictory market data must fail closed.
- Use closed candles for confirmed analysis. Never synthesize missing OHLCV.
- Preserve the existing PostgreSQL paper ledger and shared market schema; do not mutate production database schema or secrets in this work.
- Preserve root/deployment-package parity where the existing CI contract requires mirrored files.
- No hosting plan, database plan, production environment, production deployment or paid resource may be changed by this plan.
- Do not claim profitability from heuristic scores or synthetic tests.

---

### Task 1: Record the master roadmap and establish an isolated implementation branch

**Files:**
- Create: `docs/SOLO_MASTER_PROJECT_ROADMAP.md`
- Create: `docs/plans/SOLO_MARKET_WATCHER_ADAPTIVE_IMPLEMENTATION.md`

**Interfaces:**
- Roadmap records existing source foundations separately from incomplete integration and runtime verification.
- Plan records exact contracts and test gates for subsequent work.

- [x] **Step 1: Inspect repository and current audit**
  
  Reviewed `PROJECT_COMPLETENESS_AUDIT.md`, deployment-status documents, worker, shared-market store, API, streaming, timeframe aggregation and tests.

- [x] **Step 2: Create isolated branch**
  
  Branch: `solo/market-watcher-adaptive-foundation`, based on `main`.

- [x] **Step 3: Add master roadmap**
  
  Record Parts 0–9, current implementation states, dependency order, acceptance gates and paper-only safety constraints.

- [ ] **Step 4: Verify roadmap is present on branch**
  
  Run: GitHub fetch of `docs/SOLO_MASTER_PROJECT_ROADMAP.md` on the feature branch.
  
  Expected: exact file content is returned and branch remains isolated from `main`.

---

### Task 2: Define and test a deterministic adaptive strategy selector

**Files:**
- Create: `backend/adaptive_strategy.py`
- Create: `tests/test_adaptive_strategy.py`
- Mirror: `extracted-source/ai-auto-trader-v4-assembly/backend/adaptive_strategy.py`
- Mirror: `extracted-source/ai-auto-trader-v4-assembly/tests/test_adaptive_strategy.py` only if package test layout requires it.

**Interfaces:**
- Input: typed market regime, timestamped timeframe states, and strategy candidates with eligibility, research score, execution-cost estimate, evidence and explicit paper-only authority flag.
- Output: one selected strategy proposal or explicit `WAIT`/ `NO_TRADE`; selection includes ranked candidates and reasons.
- Strategy families: scalping, day, swing, position, long-term analysis-only.
- Required timeframes:
  - scalping: `1m/5m`, confirmation `15m/1h`;
  - day: `5m/15m/1h`, confirmation `4h`;
  - swing: `1h/4h`, confirmation `1d/1w`;
  - position: `1d/1w`, confirmation `1M`;
  - long-term analysis: `1w/1M`, confirmation `1Y`.
- Scores are ranking heuristics, never calibrated probabilities or forecasts.
- Long-term analysis is not an executable paper-trade strategy in the initial implementation.

- [ ] **Step 1: Add failing tests**
  
  Cover: empty candidate list returns WAIT; ineligible candidates cannot be selected; stale or missing required timeframe blocks that strategy; a higher ranking score wins among eligible strategies; costs above a strategy's declared cap disqualify it; conflicting higher-timeframe context disqualifies strategies that require confirmation; long-term analysis can be selected as analysis-only but never returned as a paper execution proposal; malformed/non-finite scores and costs are rejected; every result carries `orders_authorized=False`.

- [ ] **Step 2: Verify focused failures**
  
  Run: `python -m pytest -q tests/test_adaptive_strategy.py`
  
  Expected: import/contract failures before implementation.

- [ ] **Step 3: Implement pure selector**
  
  Use immutable dataclasses and enums, UTC-aware timestamps, explicit strategy timeframe requirements, explicit regime compatibility, bounded finite scores/costs, deterministic tie-breaking, and no network/database/exchange dependencies. Do not embed risk authorization in this module.

- [ ] **Step 4: Verify focused pass**
  
  Run: `python -m pytest -q tests/test_adaptive_strategy.py`
  
  Expected: all selector tests pass; no candidate can set order authority.

- [ ] **Step 5: Verify full Python suite and source parity**
  
  Run: `python -m pytest -q tests backend/tests` and `python -m compileall -q app backend`; run the repository's root/deployment package parity checks.
  
  Expected: all existing and new tests pass; mirrored files are byte-identical.

- [ ] **Step 6: Commit the independently passing deliverable**
  
  Commit message: `feat: add paper-only adaptive strategy selector`.

---

### Task 3: Add a backend multi-timeframe market assessment contract

**Files:**
- Create: `backend/multi_timeframe_analysis.py`
- Create: `tests/test_multi_timeframe_analysis.py`
- Mirror files in `extracted-source/ai-auto-trader-v4-assembly` when the deployed package imports this module.

**Interfaces:**
- Input: one symbol, per-timeframe closed OHLCV histories, observation timestamp and freshness/history policy.
- Output: per-timeframe trend/momentum/volatility/structure evidence, higher/medium/lower timeframe context, conflicts, regime, confidence limitations, exclusions and explicit no-trade reasons.
- Distinguish `1m` from `1M`; intervals are case-sensitive.
- No mechanical timeframe majority vote.

- [ ] Add tests for timestamp order, gaps, stale/future data, incomplete candles, insufficient history, OHLCV validation, bullish/bearish/mixed structures, and conflict reporting.
- [ ] Implement deterministic calculations with transparent indicators and provenance.
- [ ] Verify the full Python suite and deployment-package parity.
- [ ] Commit only after focused and regression checks pass.

---

### Task 4: Integrate Market Watcher with durable shared snapshots and intelligence

**Files:**
- Modify only after inspecting exact runtime entry points: `backend/market_worker.py`, `backend/shared_market_store.py`, `app/engines/paper_service_app.py`, relevant API/runtime composition and tests.
- Update: `docs/MARKET_WORKER.md`, `docs/ARCHITECTURE.md`.

**Interfaces:**
- The worker publishes validated feed snapshots and heartbeats using existing PostgreSQL leases.
- Watcher scans use deterministic idempotent scan IDs and durable intelligence records.
- API and WebSocket read only fresh persisted snapshots and intelligence; they never start duplicate scanners.
- API/worker can restart independently.

- [ ] Add failing integration tests for lease loss, stale heartbeat, worker crash, duplicate scan replay, database outage, API restart and stale snapshot rejection.
- [ ] Implement the minimum missing integration without replacing existing store schemas or silently falling back to SQLite.
- [ ] Verify worker lease takeover and recovery against a disposable staging database only.
- [ ] Do not point tests or staging at the production database.

---

### Task 5: Expose watcher and adaptive decision state through API/WebSocket

**Files:**
- Modify: `app/engines/read_only_api.py`, `backend/streaming_contract.py`, `backend/asgi_streaming.py` only after inspecting current contracts.
- Modify focused API and streaming tests.
- Mirror deployed package files if required.

- [ ] Add versioned read-only payloads for worker heartbeat, last successful scan, market freshness, regime, ranked opportunities, selected strategy and WAIT/NO_TRADE reason.
- [ ] Keep client-to-server control messages ignored/rejected; no strategy toggles or order commands over the market stream.
- [ ] Add auth, malformed payload, stale data, reconnect, duplicate/out-of-order and unauthorized tests.
- [ ] Verify no existing endpoint contract regresses.

---

### Task 6: Wire adaptive proposals through the existing paper risk path

**Files:**
- Modify only after reviewing current runtime composition and risk contracts: `app/engines/research_pipeline.py`, `app/engines/complete_analysis_pipeline.py`, `app/engines/paper_service_app.py` and focused tests.

- [ ] The selector may propose only one eligible strategy per opportunity; it cannot create a ledger intent.
- [ ] Route every paper proposal through existing independent Risk Guardian, portfolio-level limits, idempotency and audit.
- [ ] Aggregate correlated exposure across strategies and symbols.
- [ ] Verify all failed risk checks block and log the reason.
- [ ] Preserve emergency-stop behavior and paper-only authority flags.

---

### Task 7: Connect Android Market Watch UI to stable backend contracts

**Files:**
- Modify: Android market/dashboard screens and API models only after versioned backend contracts are stable.
- Update Android unit/UI contract tests.

- [ ] Show worker status, heartbeat age, last scan, feed freshness, market regime, opportunity ranking, selected strategy and rationale.
- [ ] Show missing data and conflicts explicitly; do not fabricate live values.
- [ ] Keep analysis and status screens read-only.
- [ ] Run Android unit tests and GitHub Actions APK build; phone verification remains a separate gate.

---

### Task 8: Staging-only verification and deployment gate

- [ ] Run backend tests, compilation, parity tests, Android unit tests and APK workflow.
- [ ] Use only the isolated staging database and staging service.
- [ ] Verify market data connectivity from the target staging host.
- [ ] Run a supervised multi-day paper-only soak and worker/API restart tests.
- [ ] Verify authenticated WebSocket events and Android staging connection.
- [ ] Confirm `orders_authorized=False` in every report, event and paper result.
- [ ] Do not deploy to production, modify production secrets/settings, change hosting plans or incur charges as part of this plan.

## Execution handoff

The implementation branch is `solo/market-watcher-adaptive-foundation`. Tasks are intentionally gated: do not proceed from a failing test or unverified integration to a deployment claim. The first code deliverable is the pure adaptive selector; later tasks depend on its tested interface and the backend multi-timeframe assessment contract.
