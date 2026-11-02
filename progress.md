# Implementation progress

Plan: `extracted-source/ai-auto-trader-v4-assembly/docs/plans/2026-10-31-continuous-opportunity-engine-implementation-plan.md`

Task 1: complete

- Added private PostgreSQL opportunity, assessment, evidence, watch-condition, append-only event, and worker-claim tables.
- Added immutable repository records and a PostgreSQL store with idempotent creation, versioned state transitions, stable event ordering, pagination, owner-bound leases, and strict recursive paper-only payload checks.
- Added PostgreSQL integration tests and synchronized the root/assembly shared-market-data test fixture.
- TDD RED observed: GitHub Actions initially failed collection with `ModuleNotFoundError: No module named 'backend.opportunity_store'`.
- First GREEN attempt exposed an audit-event ordering defect when two transitions shared the same timestamp. Fixed by assigning each event the opportunity version and ordering by that version.
- Verification on the feature branch: Durable Paper Ledger workflow passed (6 ledger integration tests; full backend suite 475 passed, 13 skipped); ASGI runtime workflow passed; Step 3 source-parity/market-data validation workflow passed.
- The PostgreSQL tests ran against disposable GitHub Actions PostgreSQL services. The Supabase database was not migrated or modified.
- No deployment, hosting, secret, billing, or live-trading configuration was changed.

Task 2: complete

- Added independent, versioned assessments for scalping, day, swing, position and long-term analysis.
- Reused the timeframe requirements already defined by the existing adaptive strategy module; did not introduce a new timeframe schedule.
- Preserved strategy disagreement and per-strategy decisions without a majority vote.
- Long-term analysis is structurally analysis-only and cannot become risk-review or paper eligible.
- Missing evidence and assessor exceptions fail closed; scores remain heuristic and NOT_CALIBRATED.
- TDD RED observed: initial CI collection failed because backend.strategy_assessment_engine did not exist.
- Verification: Durable Paper Ledger workflow passed (6 PostgreSQL ledger tests; full backend suite 487 passed, 13 skipped); ASGI runtime and Step 3 source-parity/market-data validation passed.
- No live order capability, database migration application, deployment, hosting, secrets, or billing changes were made.


Task 3: complete (search handler implemented; worker activation is Task 5)

- Added a continuous Testnet search handler that reuses the existing shared worker's market source and existing multi-timeframe scan; no second exchange client or polling loop was introduced.
- Each symbol now gets five independent strategy assessments persisted inside the existing durable market-intelligence report, including evidence references, per-strategy reasons, calibration status, and paper-only flags.
- The search handler returns coverage and candidate counts, enforces Futures Testnet-only configuration, and preserves bounded timeframe fetch concurrency.
- Durable opportunity rows and waiting conditions are deliberately deferred to Task 4: a scan-only heuristic does not yet contain an approved trigger/price plan, so creating registry rows now would risk duplicate or misleading setup identities.
- TDD RED observed: initial CI collection failed because backend.continuous_opportunity_search did not exist.
- Verification: Durable Paper Ledger workflow passed (6 PostgreSQL ledger tests; full backend suite 491 passed, 13 skipped); ASGI runtime and Step 3 market-data/source-parity workflows passed.
- The new handler is not yet registered in the shared worker scheduler; that is Task 5. Continuous runtime is not active until worker integration and a verified always-on host.


Task 4: complete (durable multi-opportunity coordinator implemented and CI verified)

- Implemented durable same-symbol/multi-symbol independent watches, explicit trigger and invalidation conditions, expiry, cancellation, expected-version transitions and append-only audit events.
- Added worker leases with exclusive concurrent claims and safe expiry/reclaim behavior.
- Stale updates, invalid price plans, missing evidence, non-numeric ticks and provider failures fail closed.
- Long-term analysis cannot enter the paper-eligible waiting/trigger lifecycle.
- Explicit price-plan evidence is persisted separately from market-snapshot/candle references.
- PostgreSQL tests verify concurrency and lease reclamation. The ledger lifecycle test verifies `PAPER_OPEN` is rejected without a ledger-confirmed position; risk-approved intent remains `INTENT_RECORDED`.
- Latest full backend suite: 526 passed; PostgreSQL ledger/opportunity integration, ASGI and read-only market-data/safety checks passed.
- Coordinator remains inactive until Task 5 registers it with the supervised worker and connects fresh validation, Risk Guardian, ledger and read-only API.

Task 5: not started (worker, fresh validation, risk/ledger and read-only API integration).

---

# Part 5 — Indicator Intelligence progress

**Plan:** Solo Parts 5–7 architecture and Part 5 implementation plan reviewed in ChatGPT.  
**Branch:** `feat/continuous-opportunity-engine` (rebased/merged with latest `fix/futures-context-mainline`; no longer behind).

## Task 0 — Existing opportunity engine reconciliation: complete

- Reconciled the feature branch with the latest integration branch. The branch is now ahead by its feature commits and behind by zero commits.
- Inspected PR #74 and the complete-backend CI failure.
- Fixed the missing durable price-plan evidence record. Each waiting opportunity now stores an idempotent `PRICE_PLAN` evidence record with the explicit entry, stop, target, trigger and cost payload; `orders_authorized=false`.
- Strengthened the PostgreSQL coordinator test to verify the explicit price-plan evidence.
- Verification before the integration-branch reconciliation: full backend suite 522 passed; PostgreSQL ledger and opportunity registry integration checks passed; ASGI and read-only safety checks passed.
- After merging the latest integration branch, full backend tests, PostgreSQL integration tests, ASGI checks, read-only market-data/safety checks, offline backtest/safety checks, and Android debug APK build all passed.
- No Supabase migration was applied; no deployment, hosting, secret, billing or trading-permission changes were made.

## Task 1 — Deterministic Indicator Intelligence: implementation and CI verified

- Added `backend/indicator_intelligence.py` with validated closed-candle input and deterministic EMA20/50, RSI14, MACD 12/26/9, Bollinger Bands, ATR14, ATR percentage, VWAP and relative volume (prior 20 candles).
- Added explicit per-indicator history requirements and unavailable reasons. Invalid, stale, incomplete or gapped candle series fail closed.
- Added directional evidence and confluence reporting that preserves disagreements; neutral readings do not automatically cancel agreement from other directional indicators.
- All outputs are marked `NOT_CALIBRATED` and `orders_authorized=false`.
- Integrated per-timeframe indicator assessments into the existing scheduled market-intelligence report using the existing shared market-data feed. No second exchange client or polling loop was added.
- Added tests for numerical outputs, insufficient history, invalid candle sequences, paper-only flags and scheduled-scan report integration.
- TDD RED observed: initial test run failed collection because `backend.indicator_intelligence` did not exist. An intermediate confluence test also exposed an invalid constant-slope fixture; the fixture was corrected to represent accelerating momentum rather than weakening MACD momentum.
- Latest verified CI for the integrated change: full backend suite **525 passed**; PostgreSQL integration, ASGI runtime and read-only market-data/safety checks passed.
- This is CI verification only. No live deployment or Android phone verification was performed for the new indicator report fields.

## Remaining Part 5 work

- Task 2: feed indicator evidence into independent Nexus strategy assessments without majority-voting away disagreements.
- Task 3: finish and verify durable multi-opportunity lifecycle behavior and price-plan/trigger validation.
- Task 4: integrate supervised scheduled jobs, fresh trigger validation, read-only API visibility and the existing independent Risk Guardian/paper ledger boundary.
- Task 5: end-to-end restart, duplicate-delivery, fail-closed, API and worker recovery verification.
- Continuous runtime remains inactive until worker integration and an approved, verified always-on host.

## Task 2 — Indicator-aware Nexus strategy evidence: implementation and CI verified

- Extended `StrategyCandidate` to accept validated per-timeframe `IndicatorAssessment` records and reject mismatched symbols/timeframes or unsafe order-authority flags.
- The scheduled market-intelligence scan now passes its indicator assessments to the five independent strategy profiles: scalping, day, swing, position and long-term.
- Each strategy assessment payload retains compact indicator evidence, calibration status, unavailable reasons and mixed-timeframe indicator conflicts.
- Indicator evidence is context, not a majority vote. It does not overwrite a strategy's independent decision or bypass the Risk Guardian.
- Fail-closed strategy assessment records also preserve the indicator evidence available when an assessor throws an exception.
- TDD RED observed: the integration test initially failed with `KeyError: '1h'` because the market scan had not yet passed indicator evidence into `StrategyCandidate`. The scan handoff was then implemented.
- A separate failure-record evidence test was added before the failure-path fix; a distinct red CI run for that test was not captured. The subsequent full suite passed.
- Latest verified CI at this checkpoint: full backend suite **526 passed**; PostgreSQL ledger/opportunity integration tests passed; ASGI and read-only market-data/safety checks passed.
- No deployment, Supabase migration, secrets, hosting, billing or trading-permission changes were made.

## Remaining Part 5 work

- Task 3: complete and verify the durable multi-opportunity lifecycle, including fresh trigger validation and explicit price-plan lifecycle semantics.
- Task 4: integrate supervised scheduled jobs, fresh trigger validation, read-only API visibility and the independent Risk Guardian/paper-ledger boundary.
- Task 5: end-to-end restart, duplicate-delivery, fail-closed, API and worker-recovery verification.
- Continuous runtime remains inactive until worker integration and an approved, verified always-on host.


## Dual-venue research worker integration — implementation checkpoint (2026-11-02)

- Added a Spot-specific public market-universe reader using Spot exchange metadata, 24-hour ticker and book-ticker endpoints. Spot contract metadata is labelled `SPOT`; Futures remains `FUTURES`.
- Spot adapter defaults to Binance Spot Testnet (`https://testnet.binance.vision`); it does not use the production Spot data host. Spot candles and last-trade price remain read-only.
- Added Spot-specific market-universe tests and Spot Testnet scan/venue-pair tests.
- Extended continuous opportunity research validation to accept only the explicit pairs `SPOT/BINANCE_SPOT_TESTNET` and `FUTURES/BINANCE_USDM_TESTNET`.
- Registered separate scheduled research scans for Spot and Futures in the supervised shared market worker. Both use the durable scheduler/dispatcher and existing shared report store; no second polling loop was added to the paper runtime.
- Kept the worker's Spot source separate from the Futures source so Spot last-trade prices cannot be interpreted as Futures mark/funding data.
- Synchronized root/assembly mirrored market adapter and worker files. CI caught a missing root mirror for the new search module and a premature import; both were corrected. The latest observed ASGI workflow passed; full test/build, PostgreSQL and read-only safety workflows were still running at the latest check.
- **Important limitation:** this is scheduled dual-venue market intelligence/opportunity research, not yet a fully active durable opportunity lifecycle. The coordinator is not yet wired to venue-scoped reassessment/price-plan refresh, and triggered opportunities are not yet connected end-to-end to fresh strategy revalidation, the independent Risk Guardian, the existing paper signal cycle/ledger, and authenticated read-only opportunity APIs. The current coordinator's reassessment provider groups by symbol without venue identity, so running Spot and Futures reassessment together would risk evaluating a watch against the wrong venue. This must be fixed before calling the Continuous Opportunity Engine operational.
- No Supabase migration was applied, no deployment or hosting change was made, and no exchange permissions or order-authority settings were changed. All research outputs continue to require `orders_authorized=false`.


## Venue-isolated opportunity runtime and read-only API — verified checkpoint (2026-11-02)

This checkpoint supersedes the earlier limitation above that venue-scoped reassessment and the opportunity API were not yet wired.

- `OpportunityCoordinator` now has an explicit venue scope. A Spot coordinator can register/list/claim only `SPOT / BINANCE_SPOT_TESTNET` records; a Futures coordinator can register/list/claim only `FUTURES / BINANCE_USDM_TESTNET` records.
- PostgreSQL `list_opportunities` and `claim_due_opportunities` support strict market-type/venue filters. Partial or mismatched venue pairs are rejected.
- The shared supervised worker now registers separate durable reassessment jobs and handlers for Spot and Futures. Each handler is bound to its own read-only market source and checks the job's venue identity before fetching data.
- Reassessment now groups by symbol **and timeframe**. The provider fetches the latest closed candle for the exact opportunity timeframe, validates its age, and supplies its close to the watcher. This allows candle-close triggers to be evaluated against the intended timeframe rather than an unrelated 1-minute candle.
- Added authenticated GET-only opportunity endpoints: `/api/v1/opportunities`, `/api/v1/opportunities/{id}`, and per-opportunity `/events`, `/assessments`, and `/evidence`. Venue filters must be supplied as a valid pair. The API exposes no opportunity mutation route.
- Added worker, venue isolation, timeframe-specific reassessment, and API authentication/read-only tests.
- Root and packaged-assembly mirrors were synchronized.
- GitHub checks on commit `165749c386a8cc8d9c8d9e72a7b2d10866854488` passed: `test`, `postgres-ledger-tests`, `asgi-runtime`, `Read-only market data and analysis safety tests`, and `Offline backtest tests and safety checks`.
- **Part 5 is not yet accepted as complete.** The scheduled search currently publishes independent strategy assessments; it does not yet automatically convert eligible assessments into durable opportunities with validated price plans. Existing waiting opportunities are reassessed by venue/timeframe, but automatic plan refresh/replacement, crash-safe refresh evidence, and triggered-opportunity revalidation through the existing paper signal cycle and independent Risk Guardian remain outstanding. Those are required before claiming end-to-end opportunity activation.
- No deployment, Supabase migration, credential change, paid provisioning, or exchange-permission change was made. Both venues remain read-only; all research/coordinator outputs must keep `orders_authorized=false`.
