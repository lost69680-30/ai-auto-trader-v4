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

## Current integrated checkpoint — 2026-11-03 (supersedes earlier "Task 5 not started" entries)

The latest Parts 5–7 integration candidate is PR #97, based on the verified Part 4 branch. Exact tested head after the additional regression coverage: `94c6d19d3661502641282ae92c2adda24d8edb83`.

### Part 5 — Continuous Opportunity Engine

- Durable PostgreSQL opportunity registry, append-only events/evidence, stable idempotency, expiry, plan refresh and transactional state transitions are implemented in the integration branch.
- Independent strategy assessments remain separate; long-term is analysis-only.
- Spot and Futures Testnet scans/reassessment are venue-isolated. Spot remains research-only.
- Futures triggers are reloaded and revalidated with fresh market/candle evidence and all five strategy assessments, then pass through signal handoff, PaperServiceApplication, PaperSignalCycle, TradingOrchestrator and the independent Risk Guardian.
- Paper intent state is recorded only after ledger confirmation; PAPER_OPEN requires a matching ledger-confirmed simulated position. Duplicate or uncertain handoffs fail closed.
- Authenticated opportunity list/detail/event/assessment/evidence endpoints are GET-only.
- Worker scan/reassessment activation and paper handoff remain opt-in. `PAPER_MARKET_INTELLIGENCE_ENABLED` defaults false and `PAPER_OPPORTUNITY_PAPER_HANDOFF_ENABLED` defaults false.

### Part 6 — Conservative evaluation

- Net P&L/cost/expectancy/profit-factor/win-rate/loss-streak metrics and chronological walk-forward evaluation are integrated.
- Validation folds are disjoint; the final holdout is reserved and reported separately.
- No parameter tuning or automatic strategy promotion is included.
- Next-open, one-bar-hold fills and simplified fee/slippage/funding assumptions are disclosed. Stops, liquidation, order-book depth and market impact are not modelled; results are not profitability forecasts.

### Part 7 — Portfolio diagnostics

- Pairwise Pearson correlation uses exact shared timestamps only.
- Missing observations are not forward-filled or interpolated; insufficient overlap and zero variance remain undefined.
- Malformed/non-finite observations fail validation; diagnostics remain research-only with `orders_authorized=false`.

### Verification on exact head `94c6d19d3661502641282ae92c2adda24d8edb83`

All six applicable GitHub Actions workflows passed:
- Market-data/analysis safety: 108 passed, 13 skipped.
- ASGI runtime: 37 focused tests passed; full backend suite 540 passed, 46 skipped.
- Durable PostgreSQL ledger integration: 6 passed; opportunity registry integration: 14 passed; full backend suite 599 passed.
- Second durable-ledger workflow: full backend suite 599 passed.
- Offline backtest safety: 28 passed.
- Extended-timeframe/Android workflow: 540 passed, 46 skipped; Gradle build successful and APK artifact uploaded.

Root and extracted-assembly worker, API and new regression-test mirrors were checked byte-for-byte. The APK artifact is available from GitHub Actions run [41581443625](https://github.com/lost69680-30/ai-auto-trader-v4/actions/runs/41581443625); it has not yet been installed and phone-tested.

### Still not operationally accepted

- No Render worker deployment or always-on host verification has been performed for this integration.
- The opportunity migration remains source-only; the user's Supabase database has not been migrated.
- Migration rollback, full process-crash/lease-expiry drills, multi-day paper soak, backup/restore, and operational alerting still require separate verification.
- The new integration APK has not been phone-tested.
- This scope covers Binance Spot and USDⓈ-M Futures Testnet research only; it does not implement Forex.
- No live exchange order interface, live trading, secret change, paid hosting, or billing action was introduced.

Do not enable the research or paper-handoff flags as part of CI. Keep both opt-in flags false until the separate operational acceptance gate is satisfied.

## Part 8 — Operational Recovery and End-to-End Validation (2026-11-03)

**Implementation branch:** `feat/solo-part8-operational-recovery`  
**Draft PR:** [#101 — Part 8 operational recovery](https://github.com/lost69680-30/ai-auto-trader-v4/pull/101)  
**Base:** PR #97 branch `feat/solo-parts5-7-professional-integration`  
**Verified code/test head:** `686a13ec404ad8d7a6090af4be0062a3aaa272ae`

### Implemented

- Added a fail-safe worker shutdown helper. If the scheduled-job thread remains alive after its bounded shutdown grace, the process no longer releases the PostgreSQL worker lease or closes the shared paper ledger. The process exits and PostgreSQL lease expiry governs recovery; this prevents a replacement worker from acquiring ownership while the old process may still be executing.
- Added shutdown regressions for normal shutdown, stuck background thread, no job thread, and lease-release failure. The four tests first failed before the helper existed, then passed after implementation.
- Added a disposable-PostgreSQL market-worker lease-expiry test: a second owner is blocked before expiry, acquires after expiry, and the old owner cannot heartbeat after ownership changes. Existing durable-scheduler tests already cover expired job-lease reclaim, stale-owner completion rejection, and idempotent retry/completion.
- Added an end-to-end PostgreSQL paper-service test that asserts the actual `PostgresPaperLedger` backend is selected, a paper-only signal passes through the existing research/Risk Guardian path, Emergency Stop cancels the pending intent, the stop/audit state survives service recreation, and new intents remain blocked after restart.
- Added a 168-hour deterministic synthetic replay regression: 2,016 intents, fills, protective exits and simulated restarts; zero duplicates; no open positions; Emergency Stop inactive at the end; `orders_authorized=false`.
- Added the PostgreSQL runtime integration test to the durable-ledger GitHub Actions workflow.
- Updated `docs/PAPER_SERVICE.md` and the packaged documentation with shutdown/recovery semantics, the 168-hour replay command, and staging-only backup/restore, alerting, real-market soak, deployment, and Android acceptance gates.
- Kept all relevant root and extracted-assembly source/test/documentation mirrors byte-identical.

### CI verification

All listed results below are for code/test head `686a13ec404ad8d7a6090af4be0062a3aaa272ae`, except the offline backtest workflow, which ran on the preceding root-source commit with no later production-code change.

- [Durable PostgreSQL integration workflow](https://github.com/lost69680-30/ai-auto-trader-v4/actions/runs/41583879955): 6 PostgreSQL ledger tests passed; 1 PostgreSQL paper-service recovery test passed; 14 opportunity-registry tests passed; full backend suite **606 passed**.
- [ASGI runtime workflow](https://github.com/lost69680-30/ai-auto-trader-v4/actions/runs/41583879918): 37 focused tests passed; full suite **545 passed, 48 skipped**.
- [Market-data and analysis safety workflow](https://github.com/lost69680-30/ai-auto-trader-v4/actions/runs/41583879924): **108 passed, 14 skipped**; source mirror parity checks passed.
- [Offline backtest workflow](https://github.com/lost69680-30/ai-auto-trader-v4/actions/runs/41583661763): 28 focused tests passed; full suite **545 passed, 48 skipped**.
- The full durable-paper-ledger suite also completed successfully with **606 passed** on the verified code/test head.

The initial PostgreSQL end-to-end scenario first returned `WAITING_FOR_TRIGGER` because the test fixture regenerated a different closed-candle identity. The fixture was corrected to keep the same closed candle while advancing only the mark price; the focused PostgreSQL test then passed. No production runtime behavior change was required for that test-fixture correction.

### Not operationally accepted

- PR #101 remains draft and unmerged. PR #97 remains the Parts 5–7 foundation; no merge or deployment was performed.
- No Render/Railway or staging deployment, always-on host verification, or real multi-day public-market-data soak.
- No production Supabase migration, backup/restore rehearsal, migration rollback drill, or alert-delivery verification.
- The new integration APK has not been installed or phone-tested.
- No Forex implementation, live exchange order interface, live orders, secret changes, paid provisioning, or billing action.

**Safety state preserved:** paper-only/read-only; `PAPER_ONLY`; `orders_authorized=false`; independent Risk Guardian remains the authorization gate; research and opportunity paper-handoff flags remain disabled by default. Do not enable flags or deploy as part of this PR.


## Part 8 staging-readiness checkpoint (2026-11-03)

**Branch:** `feat/solo-part8-operational-recovery`  
**Draft PR:** [#101 — Solo Part 8 operational recovery](https://github.com/lost69680-30/ai-auto-trader-v4/pull/101)  
**Latest verified PR head:** `6784ddf6960486dca5873749c74760b720d3e65b`

### Implemented

- Added mirrored `backend.staging_preflight` commands for separate `worker` and `api` processes.
- Worker preflight requires PostgreSQL configuration, explicit Futures Testnet selection, a 1m archive interval, and a valid `PaperServiceConfig` including the required settings and equity/risk/exposure caps.
- API preflight requires API-only mode, Futures Testnet, a 32-character minimum bearer token, authenticated WebSocket transport, matching API/worker symbol allowlists and 1m intervals, and loopback binding or declared HTTPS termination.
- Both profiles fail closed if research, opportunity paper handoff, research-worker, or supervisor opt-in flags are enabled.
- Added optional `--check-database`: read-only PostgreSQL catalog inspection of all 21 required private tables, row-level security status, and remote TLS negotiation. It does not apply migrations or inspect trading rows.
- Added staging process commands and a runbook to both root and packaged documentation.
- CI now applies all repository migrations only to its disposable PostgreSQL service and executes both role preflights plus the read-only database schema check.

### Fresh CI verification on PR head `6784ddf6960486dca5873749c74760b720d3e65b`

- Staging preflight unit tests: **11 passed**.
- Worker configuration plus PostgreSQL schema/TLS preflight: passed.
- API configuration preflight: passed.
- PostgreSQL ledger integration: **6 passed**.
- PostgreSQL paper-service recovery integration: **1 passed**.
- Opportunity registry integration: **14 passed**.
- Full backend suite: **617 passed**.
- ASGI focused suite: **37 passed**; full suite: **556 passed, 48 skipped**.
- Market-data/analysis safety suite: **108 passed, 14 skipped**.
- Root and packaged preflight source, tests and runbook: byte-identical.

### Not performed

No deployment to Render, Railway or another host; no staging or production Supabase migration; no user database backup/restore; no secret or billing changes; no real multi-day market-data soak; no Android installation or phone verification. Live orders remain unavailable and unauthorized.

**Safety state:** paper-only/read-only, `PAPER_ONLY`, `orders_authorized=false`, independent Risk Guardian authority, research and opportunity paper-handoff opt-ins disabled by default.


## Read-only hosting inspection (2026-11-03)

No Render or Railway settings were changed.

### Render — My Workspace

- `solo-ai-auto-trader-staging-api`: Frankfurt Free web service, deployed from `feat/solo-ledger-dashboard-integration`, not the Part 8 branch. Its configured start command requests API-only mode, but its application logs show worker cycles, scheduled jobs, market-intelligence scans, equity snapshots, and supervisor child-process shutdown. This indicates the deployed runtime is not behaving as a clean API-only process; the exact environment flag values were not exposed by the read-only service detail tool.
- `solo-futures-websocket-staging`: Frankfurt Free web service, deployed from `fix/futures-context-mainline`, with start command `python -m backend.shared_market_supervisor`. This starts a supervisor composition, not the separated worker-only command in the current Part 8 runbook. Logs show healthy market cycles and fresh equity snapshots, then SIGTERM and child-process shutdown at 2026-11-02 20:51 UTC. The service returned no CPU, memory, or instance-count samples in the queried period. Treat it as not proven always-on.
- Both staging services are on Free web-service compute. Render documents that Free web services spin down after 15 minutes without inbound traffic; Free compute is not suitable for the continuous worker requirement.
- Legacy `ai-auto-trader-v4-backend`: Free web service on `main`; start command writes SQLite under `/tmp/v4data/paper-ledger.sqlite3`. That filesystem is ephemeral and this service is not a durable-ledger staging target.
- `solo-staging-postgres`: Free PostgreSQL in Frankfurt, created 2026-10-29, scheduled to expire 2026-11-28. The connected read-only Render SQL tool could not connect because the database IP allowlist is empty. No allowlist changes or SQL writes were attempted.
- The current Render API/worker branches are not `feat/solo-part8-operational-recovery`; PR #101 has not been deployed.

### Railway

- Connected Railway workspace `lost69680-30's Projects` currently has zero projects. No Railway project, service, volume, or deployment was created.

### Operational consequence

The existing Free Render setup cannot be accepted as the continuous staging host. This was the earlier paid-host recommendation and is superseded by the user's strict $0 instruction. Do not provision paid compute. The free-only operating decision and limitations are recorded below in “Free-only hosting decision and constraints (2026-11-03).”


## Free-only hosting decision and constraints (2026-11-03)

**User instruction:** No payment. Do not upgrade plans, create paid resources, add billing methods, or accept usage that can trigger charges. No infrastructure changes were made as part of this assessment.

### Read-only Supabase status

- The connected Supabase account currently lists two projects: `solo-ai-auto-trader` and `solo-ai-auto-trader-staging`; both reported `ACTIVE_HEALTHY` on 2026-11-03.
- The staging project currently reports only five applied migrations: paper ledger, shared market data, durable scheduled jobs, candle archive, and market-intelligence reports.
- The staging project is therefore **not yet proven to contain the Part 5–8 opportunity/recovery schema**. Its public schema table listing was empty; the migration list is the reliable observation here. Do not deploy the Part 8 worker against this database or run migrations until the expected private-schema objects, backup/restore path, and rollback procedure are verified.
- The connector did not expose the current Supabase billing plan. Project health is not proof of plan, quota, or cost status. No billing settings were inspected or changed.
- Supabase documents 500 MB database storage on Free and automatic pausing after a week of low activity. A few real requests per day may help but are not guaranteed to prevent pausing. Source: https://supabase.com/kb/faqs/platform/free-project-pausing and https://supabase.com/kb/faqs/platform/database-only-cost

### Free-tier constraints verified from official documentation

- Render Free web services sleep after 15 minutes without inbound traffic, can take about a minute to wake, have ephemeral local filesystems, and cannot run background-worker service types. Free Render Postgres expires 30 days after creation. Source: https://render.com/docs/free
- Railway Free provides $1 of monthly credit after the initial trial, with 0.5 GB RAM / 1 vCPU service limits. It is not a reliable continuous-worker plan. Source: https://docs.railway.com/pricing/plans
- GitHub Actions schedules can run no more frequently than every five minutes, but scheduled events can be delayed or dropped during load; public-repository schedules can be disabled after 60 days without repository activity. Use it for CI, manual jobs, and noncritical batch research only—not as a real-time scheduler or always-on worker. Sources: https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax and https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
- Cloudflare Workers Free allows 10 ms CPU per HTTP or Cron invocation (although Cron has a 15-minute wall-clock ceiling). It is suitable for lightweight routing/automation, not this Python analysis engine or continuous worker. Source: https://developers.cloudflare.com/workers/platform/limits/

### Approved design direction under a strict $0 budget

1. **Persistent data:** Prefer the existing dedicated Supabase staging project for staging only, after its schema, RLS, TLS, backup/restore and migration rollback are verified. Keep the production Supabase project untouched. Do not use the expiring Render Free Postgres instance as the durable ledger.
2. **Interactive API:** Keep at most one Render Free web service for development/foreground app access. Expect cold starts and intermittent availability. The API must remain API-only; it must not secretly launch the market worker or supervisor.
3. **Market-data connection:** Treat WebSockets as foreground/best-effort only. Android must reconnect after sleep/network loss, show last-update age, and mark data stale until a fresh validated snapshot arrives. No background worker may infer freshness from a connected socket alone.
4. **Scheduled analysis:** Use GitHub Actions only for CI, manually requested runs, and low-frequency noncritical batch research after a safe workflow is designed. Do not use Actions cron as a substitute for a durable worker, a one-minute market watcher, or order execution.
5. **No continuous worker under $0:** The supervised Python worker, durable one-minute scheduling, multi-day unattended market soak, and guaranteed uninterrupted WebSockets remain unavailable on the currently verified free hosting options. Do not emulate always-on service by sending keep-alive traffic or split the runtime across providers.
6. **Safe operating mode:** Keep research and opportunity paper-handoff opt-ins disabled. Keep `PAPER_ONLY`, `orders_authorized=false`, independent Risk Guardian authorization, Emergency Stop, and fail-closed stale-data checks. If the API, database, schedule, or market data is unavailable/stale, show degraded/offline state and block new paper intents.
7. **Future gate:** Reconsider hosting only if a genuinely free, terms-compliant, persistent compute option is verified. Any option requiring a card, paid trial, subscription, usage-based charges, or a possible overage is excluded unless the user explicitly changes the no-payment instruction.

### Explicitly not performed

No Render/Railway service creation or configuration change; no Supabase project/branch creation, migration, pause/restore, or billing change; no GitHub deployment workflow activation; no secret rotation; no production data movement; no APK release; no live exchange orders. This is a free-tier architecture decision and readiness assessment, not an operational deployment acceptance.


### Additional zero-cost guardrails verified

- The connected GitHub repository `lost69680-30/ai-auto-trader-v4` is public. GitHub documents standard GitHub-hosted runner usage as free for public repositories. This makes CI and manually dispatched verification jobs a useful $0 tool; scheduled jobs are still non-guaranteed and must not be treated as a continuous worker. Source: https://docs.github.com/en/actions/concepts/billing-and-usage
- Render Free services count against monthly included outbound bandwidth; Render documents that exceeding the included amount can incur supplementary charges. The current account's usage/billing controls were not inspected, so a Free compute label alone is not a guarantee of zero charges. Keep traffic modest, do not create artificial keep-alive traffic, and do not add high-volume polling. Source: https://render.com/docs/free
- The connected Supabase project-detail response did not expose the billing plan or usage. Do not infer the current plan from `ACTIVE_HEALTHY`; verify the dashboard's Free plan and quotas before relying on the database for continued operation. No billing settings were changed.
