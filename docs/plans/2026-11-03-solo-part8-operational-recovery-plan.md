# Solo Part 8 — Operational Recovery and End-to-End Validation Implementation Plan

> **For agentic workers:** Use the host's available task-by-task implementation workflow. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Solo's paper-only runtime fail safely through worker shutdown, lease expiry, retry/replay, durable-state recovery, and end-to-end operational checks without deploying or enabling automation.

**Architecture:** Extend the existing PostgreSQL-backed worker and paper-ledger composition rather than adding another runtime or scheduler. Keep market polling, durable scheduled jobs, the paper ledger, the independent Risk Guardian, and the read-only API as separate components with explicit ownership. Exercise process and database failure paths using disposable CI databases and deterministic replay; never use the user's production Supabase database.

**Tech Stack:** Python 3.11+, pytest, PostgreSQL disposable CI service, existing GitHub Actions workflows, SQLite replay harness, existing PostgreSQL worker lease and durable scheduler.

## Global Constraints

- Paper-only/read-only at all times: `PAPER_ONLY`, `orders_authorized=False`; no live order interface or exchange credentials.
- The independent Risk Guardian remains the only paper-intent authorization gate. AI, strategy, worker, dispatcher, RL/GAT, and research components cannot bypass it or mutate risk limits.
- Emergency Stop remains durable, fail-closed, and operator-controlled. No automatic clearing.
- Keep `PAPER_MARKET_INTELLIGENCE_ENABLED=false` and `PAPER_OPPORTUNITY_PAPER_HANDOFF_ENABLED=false` by default.
- Preserve existing Supabase PostgreSQL schema and GitHub deployment setup. Do not apply migrations to the user's database, change secrets, deploy Render/Railway, or provision paid resources.
- Keep root and `extracted-source/ai-auto-trader-v4-assembly` runtime/test mirrors byte-identical wherever both copies exist.
- Report implemented, CI-tested, deployed, and phone-verified states separately. No deployment or phone verification is part of this plan.
- A deterministic synthetic replay is a reliability test, not a live-market soak, strategy backtest, or profitability evidence.

---

### Task 1: Fail-safe worker shutdown when scheduled work outlives its grace period

**Files:**
- Modify: `backend/shared_market_worker.py`
- Modify: `tests/test_shared_market_worker.py`
- Mirror: `extracted-source/ai-auto-trader-v4-assembly/backend/shared_market_worker.py`
- Mirror: `extracted-source/ai-auto-trader-v4-assembly/tests/test_shared_market_worker.py`

**Interfaces:**
- Consumes: existing `main()` lifecycle, `threading.Event`, scheduled-job thread, `PostgresMarketFeedStore.release()`, and `PaperServiceApplication.close()`.
- Produces: private lifecycle helper `_shutdown_worker(stop, job_runner, store, app, *, grace_seconds) -> bool`. Returns true only when background work has stopped and resources were safely released/closed.

- [ ] **Step 1: Add focused failing tests**
  - A completed scheduled-job thread is joined, then the worker lease is released and the paper application is closed.
  - A scheduled-job thread still alive after the grace timeout causes the helper to leave the worker lease unreleased and the shared ledger open; it returns false so the process can exit and the lease can expire naturally.
  - A release exception still closes the paper application after the thread has stopped.
  - A missing job thread follows the normal release/close path.

- [ ] **Step 2: Verify the relevant failure**

Run: `python -m pytest -q tests/test_shared_market_worker.py -k shutdown`

Expected: tests fail because shutdown currently releases the worker lease and closes the application even if the scheduled-job thread is still alive.

- [ ] **Step 3: Implement the minimum behavior**

Extract shutdown ordering into `_shutdown_worker`: signal stop; join a live job thread for the bounded grace period; if it remains alive, log the condition and return false without releasing the market-worker lease or closing the shared application. This deliberately leaves lease expiry to PostgreSQL after process exit, preventing a second worker from acquiring ownership while the old process may still be executing. If the thread has stopped, release the worker lease and close the application in a `finally` block. Keep the existing minimum five-second shutdown grace.

- [ ] **Step 4: Verify the focused pass**

Run: `python -m pytest -q tests/test_shared_market_worker.py -k shutdown`

Expected: all shutdown tests pass, including the still-running-thread case.

- [ ] **Step 5: Run the affected integration check**

Run: `python -m pytest -q tests/test_shared_market_worker.py`

Expected: all worker lifecycle, scheduler-health, venue-isolation, and opt-in-default tests pass.

- [ ] **Step 6: Commit the passing deliverable**

```bash
git add backend/shared_market_worker.py tests/test_shared_market_worker.py extracted-source/ai-auto-trader-v4-assembly/backend/shared_market_worker.py extracted-source/ai-auto-trader-v4-assembly/tests/test_shared_market_worker.py
git commit -m "fix: preserve worker lease during stuck shutdown"
```

### Task 2: Prove durable lease expiry and at-least-once scheduled-job recovery

**Files:**
- Inspect/modify only as required: `backend/durable_scheduler.py`, `backend/shared_market_store.py`
- Tests: `tests/test_durable_scheduler.py`, `tests/test_shared_market_store.py`
- Mirror corresponding files and tests under `extracted-source/ai-auto-trader-v4-assembly/`

**Interfaces:**
- Consumes: existing PostgreSQL scheduler lease APIs (`claim_next`, `complete`, `fail`) and market-worker ownership lease.
- Produces: tested recovery behavior proving expired claims can be reclaimed, stale owners cannot complete a reclaimed job, and repeated delivery with the same idempotency key does not duplicate durable effects.

- [ ] **Step 1: Add disposable-PostgreSQL integration tests**
  - Simulate owner A claiming a due job and disappearing before completion.
  - Advance the test database clock/lease timestamp beyond expiry, then prove owner B can reclaim the same job.
  - Prove owner A cannot complete or fail owner B's lease.
  - Complete the job as owner B and prove a subsequent dispatcher pass does not repeat the completed side effect.
  - Verify the worker ownership lease is not released by the shutdown path when the job thread remains active.

- [ ] **Step 2: Verify the relevant failure**

Run the focused PostgreSQL scheduler/worker integration tests using the repository's existing disposable PostgreSQL workflow.

Expected: missing or incorrect expiry/reclaim behavior fails with a specific ownership or duplicate-effect assertion.

- [ ] **Step 3: Implement only demonstrated gaps**

Retain at-least-once delivery. Use the existing idempotency key as the stable side-effect identity. Reject completion/failure from stale lease owners. Do not add a second scheduler, polling loop, or any execution-capable job handler.

- [ ] **Step 4: Verify the focused pass**

Run the focused scheduler and worker PostgreSQL integration tests.

Expected: lease reclaim succeeds only after expiry; stale owners cannot mutate the new lease; one durable side effect is observed.

- [ ] **Step 5: Run the affected integration check**

Run the complete durable-ledger PostgreSQL workflow and the full backend suite.

Expected: all existing ledger, opportunity registry, scheduler, API, and risk-safety tests pass.

- [ ] **Step 6: Commit the passing deliverable**

```bash
git add backend tests extracted-source/ai-auto-trader-v4-assembly/backend extracted-source/ai-auto-trader-v4-assembly/tests
git commit -m "test: verify durable worker and job recovery"
```

### Task 3: End-to-end durable paper-runtime acceptance harness

**Files:**
- Modify/create focused tests under `tests/` for the existing paper service, PostgreSQL ledger, read-only API, worker heartbeat, and Emergency Stop.
- Mirror matching tests under `extracted-source/ai-auto-trader-v4-assembly/tests/`.
- Modify: `.github/workflows/test-durable-ledger.yml` only if an existing job does not already execute the new integration tests.
- Modify: `docs/PAPER_SERVICE.md` and `progress.md` to distinguish automated evidence from operational acceptance.

**Interfaces:**
- Consumes: `PaperServiceApplication.create()`, `PostgresPaperLedger`, `PaperRuntime.process_poll()`, `PaperProtectionEngine`, authenticated API routes, and shared-worker heartbeat.
- Produces: an automated disposable-database scenario covering startup, one safe paper-only lifecycle, audit consistency, Emergency Stop, restart, and database-unavailable behavior.

- [ ] **Step 1: Add failing integration assertions**
  - Start with a disposable PostgreSQL schema and verify storage status is PostgreSQL-backed.
  - Record a paper-only intent through the existing Risk Guardian path; assert every resulting record/report keeps `orders_authorized=false`.
  - Activate Emergency Stop; assert pending intents are cancelled, new intents are blocked, and the stop is not automatically cleared.
  - Restart the service using the same disposable database; assert the stop and open-position state persist while unrecoverable pending intents are cancelled.
  - Simulate database unavailability; assert ledger-backed state-changing operations fail closed and no SQLite fallback is created.
  - Verify audit/event counts and identifiers reconcile with the ledger state after restart.

- [ ] **Step 2: Verify the relevant failure**

Run the new test against the existing disposable PostgreSQL CI service.

Expected: any missing cross-component guarantee fails at the first state or audit assertion; no production database is contacted.

- [ ] **Step 3: Implement only demonstrated gaps**

Prefer existing ledger transactions and API contracts. Do not introduce a new order endpoint, stop-clear endpoint, new risk authority, or schema migration without a separately reviewed need.

- [ ] **Step 4: Verify the focused pass**

Run the focused PostgreSQL end-to-end test and the corresponding SQLite service regression tests.

Expected: state, Emergency Stop, audit, and fail-closed behavior are consistent before and after restart.

- [ ] **Step 5: Run the affected integration check**

Run all durable-ledger, ASGI, market-data safety, and offline-backtest workflows.

Expected: every applicable workflow passes and the root/assembly mirrors match byte-for-byte.

- [ ] **Step 6: Commit the passing deliverable**

```bash
git add tests extracted-source/ai-auto-trader-v4-assembly/tests .github/workflows docs/PAPER_SERVICE.md progress.md
git commit -m "test: add end-to-end paper runtime acceptance"
```

### Task 4: Long-horizon deterministic replay and operational runbook

**Files:**
- Modify: `tests/test_paper_soak.py`
- Modify: `.github/workflows/test-durable-ledger.yml` or a dedicated paper-recovery workflow if required.
- Modify: `docs/PAPER_SERVICE.md`, `progress.md`, and matching extracted documentation where maintained.

**Interfaces:**
- Consumes: `build_deterministic_soak_events()`, `run_paper_soak()`, and the existing CLI `python -m app.engines.paper_soak --hours N`.
- Produces: a reproducible 168-hour synthetic replay acceptance result plus an operator checklist for later staging-only live-data soak, backup/restore, alerting, and phone verification.

- [ ] **Step 1: Add a failing long-horizon replay test**
  - Run a 168-hour deterministic replay in a fresh temporary SQLite database.
  - Assert 2,016 paper intents, 2,016 simulated fills, 2,016 protective exits, zero duplicate intents, zero open positions, 168 restart events, no active Emergency Stop, and `orders_authorized=false`.
  - Assert the report labels its P&L as scripted synthetic output, not profitability evidence.

- [ ] **Step 2: Verify the relevant failure**

Run: `python -m pytest -q tests/test_paper_soak.py -k 168_hour`

Expected: the test fails until the long-horizon replay is included in the automated acceptance suite or the runner exposes a required report field.

- [ ] **Step 3: Implement the minimum verification**

Keep the deterministic event generator and replay harness unchanged unless a test demonstrates a correctness defect. Add a bounded CI invocation for the 168-hour synthetic scenario. Document separate staging-only gates for a real multi-day paper soak, verified database backup/restore, alert delivery, deployment health, and Android phone acceptance; do not claim those operational gates have passed.

- [ ] **Step 4: Verify the focused pass**

Run: `python -m pytest -q tests/test_paper_soak.py -k 168_hour`

Expected: all long-horizon replay invariants pass.

- [ ] **Step 5: Run the affected integration check**

Run the full backend suite and the full set of applicable GitHub Actions workflows.

Expected: no regressions; workflow output reports exact pass/skip counts and the APK artifact remains a separate phone-installation gate.

- [ ] **Step 6: Commit the passing deliverable**

```bash
git add tests .github/workflows docs/PAPER_SERVICE.md progress.md extracted-source/ai-auto-trader-v4-assembly
git commit -m "test: add long-horizon paper recovery replay"
```

## Unresolved operational decisions

- The production host and worker topology are not being changed in Part 8. A separate acceptance decision is required before deploying an always-on worker.
- The user's Supabase migration and backup/restore rehearsal must be performed only after explicit approval and on a verified backup/restore target.
- A true multi-day market-data paper soak requires an approved staging deployment and is deliberately not represented by the deterministic 168-hour replay.
- The new Android APK still requires installation and phone-level verification; CI build success is not device acceptance.
