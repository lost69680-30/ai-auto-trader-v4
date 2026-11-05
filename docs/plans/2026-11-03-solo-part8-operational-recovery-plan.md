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

- [x] **Step 1: Add focused failing tests**
  - A completed scheduled-job thread is joined, then the worker lease is released and the paper application is closed.
  - A scheduled-job thread still alive after the grace timeout causes the helper to leave the worker lease unreleased and the shared ledger open; it returns false so the process can exit and the lease can expire naturally.
  - A release exception still closes the paper application after the thread has stopped.
  - A missing job thread follows the normal release/close path.

- [x] **Step 2: Verify the relevant failure**

Run: `python -m pytest -q tests/test_shared_market_worker.py -k shutdown`

Expected: tests fail because shutdown currently releases the worker lease and closes the application even if the scheduled-job thread is still alive.

- [x] **Step 3: Implement the minimum behavior**

Extract shutdown ordering into `_shutdown_worker`: signal stop; join a live job thread for the bounded grace period; if it remains alive, log the condition and return false without releasing the market-worker lease or closing the shared application. This deliberately leaves lease expiry to PostgreSQL after process exit, preventing a second worker from acquiring ownership while the old process may still be executing. If the thread has stopped, release the worker lease and close the application in a `finally` block. Keep the existing minimum five-second shutdown grace.

- [x] **Step 4: Verify the focused pass**

Verification: the shutdown regression tests were included in the full backend suite, which passed with 606 tests on code/test head `686a13ec404ad8d7a6090af4be0062a3aaa272ae`. The focused `-k shutdown` subset was not run as a separate command.

- [x] **Step 5: Run the affected integration check**

Verification: the full backend suite passed, including all worker lifecycle, scheduler-health, venue-isolation, and opt-in-default tests.

- [x] **Step 6: Commit the passing deliverable**

```bash
git add backend/shared_market_worker.py tests/test_shared_market_worker.py extracted-source/ai-auto-trader-v4-assembly/backend/shared_market_worker.py extracted-source/ai-auto-trader-v4-assembly/tests/test_shared_market_worker.py
git commit -m "fix: preserve worker lease during stuck shutdown"
```

### Task 2: Prove durable lease expiry and at-least-once scheduled-job recovery

**Files:**
- Inspect/modify only as required: `backend/durable_scheduler.py`, `backend/shared_market_store.py`
- Tests: `tests/test_durable_scheduler.py`, `tests/test_shared_market_data_store.py`
- Mirror corresponding files and tests under `extracted-source/ai-auto-trader-v4-assembly/`

**Interfaces:**
- Consumes: existing PostgreSQL scheduler lease APIs (`claim_next`, `complete`, `fail`) and market-worker ownership lease.
- Produces: tested recovery behavior proving expired claims can be reclaimed, stale owners cannot complete a reclaimed job, and repeated delivery with the same idempotency key does not duplicate durable effects.

- [x] **Step 1: Add disposable-PostgreSQL integration tests**
  - Reuse the existing PostgreSQL scheduler regression proving an expired job lease can be reclaimed and a stale owner cannot complete the replacement owner's claim.
  - Add a PostgreSQL market-worker lease regression proving a second owner is rejected before expiry, can acquire after expiry, and the old owner cannot heartbeat afterward.
  - Preserve the original idempotency key across scheduler retry and verify duplicate completion is rejected.
  - Verify the worker ownership lease is not released by the shutdown path when the job thread remains active.

- [x] **Step 2: Verify the relevant failure**

Run the focused PostgreSQL scheduler/worker integration tests using the repository's existing disposable PostgreSQL workflow.

Verification: the disposable-PostgreSQL workflow and full backend suite passed. The existing scheduler lease-reclaim regression and the new worker lease-expiry regression both ran in that suite.

- [x] **Step 3: Implement only demonstrated gaps**

Retain at-least-once delivery. Use the existing idempotency key as the stable side-effect identity. Reject completion/failure from stale lease owners. Do not add a second scheduler, polling loop, or any execution-capable job handler.

- [x] **Step 4: Verify the focused pass**

Run the focused scheduler and worker PostgreSQL integration tests.

Verification: the full suite passed with the lease-expiry and stale-owner assertions. No scheduler implementation change was needed because the existing lease logic already met the tested contract.

- [x] **Step 5: Run the affected integration check**

Run the complete durable-ledger PostgreSQL workflow and the full backend suite.

Expected: all existing ledger, opportunity registry, scheduler, API, and risk-safety tests pass.

- [x] **Step 6: Commit the passing deliverable**

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

- [x] **Step 1: Add failing integration assertions**
  - Start with a disposable PostgreSQL schema and explicitly assert `PaperServiceApplication` is using `PostgresPaperLedger`.
  - Record a paper-only intent through the existing research-to-Risk-Guardian path; assert the resulting report and ledger keep `orders_authorized=false`.
  - Activate Emergency Stop; assert pending intents are cancelled, subsequent intents are blocked, and the stop is not automatically cleared.
  - Restart the service using the same disposable database; assert the stop and cancelled-intent state persist.
  - Verify audit history and `verify_integrity()` after restart. Existing service tests separately cover database-initialization failure and the no-SQLite-fallback rule; existing PostgreSQL ledger lifecycle tests cover durable fills/positions.

- [x] **Step 2: Verify the relevant failure**

Run the new test against the existing disposable PostgreSQL CI service.

Verification: the new disposable-PostgreSQL paper-service integration test passed (1 passed); the same workflow also passed 6 ledger integration tests, 14 opportunity-registry integration tests, and the 606-test full backend suite. No production database was contacted.

- [x] **Step 3: Implement only demonstrated gaps**

Prefer existing ledger transactions and API contracts. Do not introduce a new order endpoint, stop-clear endpoint, new risk authority, or schema migration without a separately reviewed need.

- [x] **Step 4: Verify the focused pass**

Run the focused PostgreSQL end-to-end test and the corresponding SQLite service regression tests.

Verification: the focused PostgreSQL runtime recovery test passed and the complete backend suite passed.

- [x] **Step 5: Run the affected integration check**

Run all durable-ledger, ASGI, market-data safety, and offline-backtest workflows.

Expected: every applicable workflow passes and the root/assembly mirrors match byte-for-byte.

- [x] **Step 6: Commit the passing deliverable**

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

- [x] **Step 1: Add a failing long-horizon replay test**
  - Run a 168-hour deterministic replay in a fresh temporary SQLite database.
  - Assert 2,016 paper intents, 2,016 simulated fills, 2,016 protective exits, zero duplicate intents, zero open positions, 168 restart events, no active Emergency Stop, and `orders_authorized=false`.
  - Assert the report labels its P&L as scripted synthetic output, not profitability evidence.

- [x] **Step 2: Verify the relevant failure**

Run: `python -m pytest -q tests/test_paper_soak.py -k 168_hour`

Verification: the existing deterministic replay runner already supported the 168-hour horizon. The new regression test was added without changing replay behavior and passed in the full backend suite.

- [x] **Step 3: Implement the minimum verification**

Keep the deterministic event generator and replay harness unchanged unless a test demonstrates a correctness defect. Add a bounded CI invocation for the 168-hour synthetic scenario. Document separate staging-only gates for a real multi-day paper soak, verified database backup/restore, alert delivery, deployment health, and Android phone acceptance; do not claim those operational gates have passed.

- [x] **Step 4: Verify the focused pass**

Verification: the 168-hour replay regression ran as part of the full backend suite and passed with all 606 tests.

- [x] **Step 5: Run the affected integration check**

Run the full backend suite and the full set of applicable GitHub Actions workflows.

Expected: no regressions; workflow output reports exact pass/skip counts and the APK artifact remains a separate phone-installation gate.

- [x] **Step 6: Commit the passing deliverable**

```bash
git add tests .github/workflows docs/PAPER_SERVICE.md progress.md extracted-source/ai-auto-trader-v4-assembly
git commit -m "test: add long-horizon paper recovery replay"
```

## Unresolved operational decisions

- The production host and worker topology are not being changed in Part 8. A separate acceptance decision is required before deploying an always-on worker.
- The user's Supabase migration and backup/restore rehearsal must be performed only after explicit approval and on a verified backup/restore target.
- A true multi-day market-data paper soak requires an approved staging deployment and is deliberately not represented by the deterministic 168-hour replay.
- The new Android APK still requires installation and phone-level verification; CI build success is not device acceptance.


## Execution and verification record

- Part 8 branch: `feat/solo-part8-operational-recovery`; draft PR: [#101](https://github.com/lost69680-30/ai-auto-trader-v4/pull/101), stacked on PR #97.
- Verified code/test head: `686a13ec404ad8d7a6090af4be0062a3aaa272ae`.
- Durable PostgreSQL workflow: 6 ledger tests, 1 end-to-end PostgreSQL paper-service recovery test, 14 opportunity-registry tests, and 606 full-suite tests passed.
- ASGI workflow: 37 focused tests passed; full suite 545 passed, 48 skipped.
- Market-data/analysis safety workflow: 108 passed, 14 skipped; packaged source parity checks passed.
- The separate durable-paper-ledger full-suite workflow also passed with 606 tests.
- Offline backtest workflow passed on the preceding root-source commit: 28 focused tests and 545 full-suite tests passed, 48 skipped.
- The shutdown tests first failed as intended before the helper existed; after implementation, the full backend suite passed.
- The initial PostgreSQL runtime scenario exposed a test-fixture signal-identity mismatch (`WAITING_FOR_TRIGGER`); reusing the same closed candle while advancing only the mark price corrected the test. The corrected end-to-end test passed.
- Root and extracted-assembly worker, worker tests, market-store tests, paper-soak tests, PostgreSQL runtime integration test, and paper-service runbook were checked for byte-identical mirrors where both copies exist.

## Explicitly not complete / operational acceptance gates

- No Render/Railway or other staging/production deployment; no host-topology change.
- No production Supabase migration, backup, restore, or rollback rehearsal.
- No alert delivery or real multi-day public-market-data paper soak.
- No Android installation or phone verification of a new APK.
- No Forex support, live exchange order interface, live orders, secret changes, paid provisioning, or billing.


### Task 7: Reproducible staging configuration and database preflight

**Files:**
- Create: `backend/staging_preflight.py`
- Create: `extracted-source/ai-auto-trader-v4-assembly/backend/staging_preflight.py`
- Create: `tests/test_staging_preflight.py`
- Create: `extracted-source/ai-auto-trader-v4-assembly/tests/test_staging_preflight.py`
- Modify: `.github/workflows/test-durable-ledger.yml`
- Modify: root and packaged `docs/PAPER_SERVICE.md`

**Implemented behavior:**
- Configuration-only worker/API preflight opens no network or database connection.
- Worker requires a parseable PostgreSQL DSN, explicit Futures Testnet selection, 1m archive interval, and a successful load of the actual `PaperServiceConfig` so all required settings and equity/risk/exposure constraints are checked before readiness.
- API requires API-only role, Futures Testnet mode, a bearer token of at least 32 characters, WebSocket enabled, matching API/worker symbol allowlists and 1m intervals, and loopback binding or explicit trusted HTTPS termination.
- Both roles reject enabled research, opportunity paper handoff, research-worker, and supervisor opt-ins.
- Optional `--check-database` inspects only PostgreSQL catalogs in a READ ONLY transaction, verifies all 21 required private tables, and requires row-level security on each table. Remote connections must negotiate TLS. DSNs and tokens are redacted from diagnostics.
- CI applies all repository migrations only to its disposable local PostgreSQL service and then runs worker/API preflight and the read-only schema check.

**Verification record:**
- `tests/test_staging_preflight.py`: 11 passed.
- Disposable PostgreSQL ledger tests: 6 passed; end-to-end paper-service recovery: 1 passed; opportunity registry: 14 passed.
- Full backend suite: **617 passed**.
- ASGI focused tests: 37 passed; full suite: **556 passed, 48 skipped**.
- Market-data/analysis safety: **108 passed, 14 skipped**.
- Latest verified code/test head: `e4f3fe326ee6ed0d92b212e09b32a83d999558fa`.
- Root and packaged preflight source, tests, and runbook were verified byte-identical.

All six migration files were applied only to the disposable GitHub Actions PostgreSQL service. No user-owned database, Render/Railway service, secret, billing setting, or Android installation was accessed or changed.

**Staging acceptance still outstanding:** create/approve a separate staging database and host, rehearse provider-supported encrypted backup/restore and migration rollback, validate alerts, run a real public-market-data soak, and phone-test the matching APK. This PR does not perform or authorize those operations.


### Continuation checkpoint — 2026-11-04

Continued this existing Part 8 branch without restarting it. The opportunity-registry migration had a repeatability defect in CI because the test setup reapplies migrations while the optional backup-reader policy was already present. The migration now drops only `solo_backup_reader_select` for each affected table before recreating it.

Verified on commit `e1ef797aaa472e2eaa4b1ba24218999b8de3acbf`:
- Durable PostgreSQL integration + full backend suite: passed.
- Separate durable-ledger full backend suite: passed.
- ASGI runtime tests: passed.
- Market-data/analysis safety checks: passed.

The failure is no longer being hidden or skipped; the opportunity-registry tests pass after the migration correction. This remains CI evidence only. The operational staging, backup/restore/rollback, hosted runtime, real-market-data soak, alert delivery and Android phone acceptance gates listed above remain open, and no user-owned Supabase database or host was changed.
