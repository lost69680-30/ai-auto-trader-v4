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


### Staging database catalog verification (read-only, 2026-11-03)

A second read-only inspection targeted the actual `paper_ledger` schema in `solo-ai-auto-trader-staging` (not the empty `public` schema):

- Found **15 of the 21** tables required by `backend.staging_preflight.REQUIRED_PRIVATE_TABLES`.
- The six missing Part 5 opportunity-registry tables are: `opportunities`, `opportunity_assessments`, `opportunity_evidence`, `opportunity_watch_conditions`, `opportunity_events`, and `opportunity_claims`.
- The 15 existing tables all report RLS enabled. The catalog response showed approximately 470,317 candle rows, 2,270 market-intelligence reports, 1,121 scheduled-job rows, 63 audit events, 63 equity snapshots, and one worker heartbeat. Table counts only were returned; no trading-row payloads or credentials were read.
- Supabase's security advisor returned one INFO finding: `paper_ledger.market_intelligence_reports` has RLS enabled but no policies. Since this is a private server-side schema, do not add a broad policy merely to silence the advisor; verify schema exposure and backend-only access before any change.
- The performance advisor reported three unused indexes (INFO only). No index or schema changes were made.
- **Result:** the existing staging database is not yet compatible with the Part 8 preflight. Do not deploy the Part 8 API/worker against it and do not apply migrations until a reviewed migration/backup/rollback plan is ready. Production Supabase remains untouched.


### Read-only Supabase storage snapshot (2026-11-03)

Read-only `pg_database_size` queries returned:

- `solo-ai-auto-trader-staging`: **191 MB** total database size (200,251,059 bytes); `paper_ledger` tables total approximately 180 MB across 15 tables.
- `solo-ai-auto-trader` (production): **11 MB** total database size (11,368,115 bytes), with 9 `paper_ledger` tables.
- These are point-in-time size readings, not growth forecasts. If the staging project is on Supabase Free, 191 MB is about 38% of its 500 MB database allowance; approximately 309 MB would remain before the nominal limit. The connector did not expose the current billing plan or quota enforcement settings, so verify the plan in the Supabase dashboard before treating that remaining capacity as confirmed.
- No table data was selected by these size queries; no database writes, migrations, or billing changes were made. Keep production separate and unchanged.


### Staging migration and backup gate (read-only assessment, 2026-11-03)

- Confirmed the connected Supabase organization `lost69680-30's Org` is on the **Free** plan. The staging project is in this organization. This confirms the intended $0 Supabase plan at the organization level; no billing controls were changed.
- Confirmed the repository branch contains migration `supabase/migrations/20261031170000_create_opportunity_registry.sql`. The staging migration history stops at `20261031155011_create_market_intelligence_reports`; the opportunity-registry migration has not been applied.
- Reviewed the migration source. It creates exactly the six missing opportunity-registry tables, indexes and foreign keys; enables RLS; revokes public/anon/authenticated/service_role access; and wraps its DDL in a transaction. It contains no INSERT/UPDATE/DELETE of existing ledger rows and does not grant exchange authority. The app's direct database role and connection privileges must still be verified against the actual staging runtime before applying it.
- The six-table migration is therefore the likely schema repair, but **not applied**. First validate migration hash/source on the exact PR head, confirm backend connection role/permissions, and create an off-project logical backup.
- Supabase's official Free-plan guidance confirms Free projects have **no automatic daily database backups**. Supabase recommends manual logical exports using `supabase db dump` and off-site retention. Official sources: https://supabase.com/kb/faqs/platform/free-plan-backups and https://supabase.com/docs/guides/platform/backups.
- Backup gate: create a local/off-project encrypted backup of the staging `paper_ledger` schema and verify archive integrity; document a restore command and rehearse restore to a disposable local PostgreSQL database before any schema migration. Do not store database dumps, credentials, or connection strings in the public GitHub repository or public Actions artifacts.
- Current staging size is 191 MB (about 38% of the Free plan's 500 MB database-size quota), with roughly 309 MB of nominal headroom if quota remains unchanged. Monitor growth, especially the one-minute candle archive. This is a point-in-time measurement, not a growth forecast.
- No backup was taken by the connector; no migration was applied; no DB schema, rows, secrets, billing, Render, Railway or production settings were changed.

### Next safe execution sequence

1. Prepare a phone-compatible Termux backup runbook that prompts for the database password locally (never in chat) and saves the dump outside GitHub.
2. Verify dump integrity and restore it to local disposable PostgreSQL; do not run a restore against staging or production.
3. Inspect staging backend DB role and verify it can use the new tables while client-facing roles cannot.
4. Re-run the Part 8 preflight with `--check-database` against staging; expect it to fail until the six-table migration is applied.
5. Only after backup and permission checks pass, apply the single reviewed opportunity-registry migration to **staging only**, then verify all 21 required tables and RLS. Production remains untouched.


### Android backup runbook prepared (2026-11-03)

- Added `docs/FREE_STAGING_BACKUP_TERMUX.md` for the user's Android/Termux workflow.
- The runbook installs free `postgresql` and `age` packages, prompts for staging session-pooler details, reads the DB password through a hidden prompt (not shell history), dumps only `paper_ledger`, encrypts the archive with a separate passphrase, verifies it with `pg_restore --list`, checks SHA-256 after copying to Android shared storage, and removes the temporary plaintext dump.
- The runbook explicitly prohibits production access, credential sharing, public GitHub uploads, and restore/migration commands against staging or production.
- Archive-list validation is not a restore rehearsal. A successful restore to disposable local PostgreSQL remains mandatory before applying the missing migration.
- This is documentation only. The user has not yet run it; no backup has been created and no database was changed.


### Phone-based GitHub/AgePony staging backup (2026-11-03)

- Added `.github/workflows/solo-staging-db-backup.yml` secure manual workflow to the default `main` branch and mirrored it on this Part 8 branch. It uses a phone-held AgePony recipient, uploads only an encrypted artifact with one-day retention, caps the encrypted file at 200 MiB, and requires explicit manual confirmation of staging-only scope and available free Actions storage.
- Added `docs/FREE_STAGING_BACKUP_GITHUB.md` to `main` and this branch. It replaces the Termux-only path with Android AgePony + GitHub instructions.
- The workflow performs a pre-dump fail-closed check: exact `solo_backup_reader` identity, no superuser/BYPASSRLS/INHERIT privileges, SELECT-only table grants, no schema CREATE, RLS enabled, and the dedicated SELECT policy on every table. It then runs an age round-trip and a full restore into disposable PostgreSQL before uploading the phone-encrypted archive.
- Latest read-only Supabase catalog inspection confirmed `solo_backup_reader` exists, can log in, has `rolsuper=false`, `rolbypassrls=false`, `rolinherit=false`, and has SELECT grants on all 15 current tables. However, only 14 of 15 tables have the `solo_backup_reader_select` policy; `market_intelligence_reports` has no policy. The workflow intentionally refuses to dump until this gap is reviewed and corrected with a narrowly scoped SELECT policy. No RLS policy or database role was changed.
- GitHub standard runners are free for this public repository, but artifact storage has quotas. The workflow does not run unless the user manually confirms sufficient included free storage; if capacity cannot be verified, do not run. No billing changes or paid resources were made.
- Workflow and guide are committed on `main` (workflow safety commits `79afb98ed501ec1a30157531b1134eeb41bb93cd`, `cbe3338863892fad31302d906e8a00f0bbda0288`; guide `dcdba56ba80c101dd6f2369985986ad0c6a8d874`) and mirrored on this branch (workflow safety commits `49c54abca410e7df5d9c4f59c0433315c92f156c`, `cc7e147637ba7f9812c7ff70bfb832522a003cf4`; guide `f6962c89bb865affe26a7faf44cd36ec25d70df9`).
- This is implementation only: no workflow was run, no artifact/backup was created, no secret was added, and no staging or production database was changed. The six-table opportunity migration remains blocked until the backup and restore gates pass.


## Part 8 continuation checkpoint — 2026-11-04

Continued on the existing `feat/solo-part8-operational-recovery` branch and draft PR #101; no replacement Part 8 branch or duplicate implementation was created.

- Fixed repeatability of `20261031170000_create_opportunity_registry.sql`: the optional `solo_backup_reader_select` policy is now dropped before recreation for each opportunity table. This addresses the disposable-CI failure where repeated test setup reapplied the migration and PostgreSQL raised `DuplicateObject`.
- The regression was observed in the prior durable-ledger workflow: opportunity-registry integration tests failed on the already-existing policy. The fix was committed at `e1ef797aaa472e2eaa4b1ba24218999b8de3acbf`.
- Post-fix verification on that exact head:
  - Durable PostgreSQL workflow: success, including staging preflight, disposable migrations, read-only schema inspection, ledger tests, paper-service recovery, opportunity-registry tests, and complete backend suite: https://github.com/lost69680-30/ai-auto-trader-v4/actions/runs/41820305155
  - Durable paper-ledger full backend suite: success: https://github.com/lost69680-30/ai-auto-trader-v4/actions/runs/41820304988
  - ASGI runtime: success: https://github.com/lost69680-30/ai-auto-trader-v4/actions/runs/41820305087
  - Market-data and analysis safety: success: https://github.com/lost69680-30/ai-auto-trader-v4/actions/runs/41820304977
- These are CI results on a disposable PostgreSQL service. They do not constitute a user Supabase migration, staging deployment, real-market soak, or Android phone acceptance.
- Remaining Part 8 operational gates are unchanged: verify the intended Supabase staging schema and backup/restore/rollback evidence, host/API/worker topology acceptance under the $0 budget, alert delivery, real public-market-data soak, and phone verification of the matching APK. No paid compute, migration, secret, billing, production, or live-trading change was made.


## Authoritative backup and acceptance correction — 2026-11-04

This section supersedes earlier historical notes in this file that said the staging backup had not yet been created or restored. Those notes describe an earlier checkpoint and must not be used as the current backup status.

### Backup status — completed and user-confirmed

- The encrypted staging backup artifact `solo-staging-paper-ledger-encrypted-41805158431.zip` was successfully created and downloaded by the user.
- GitHub Actions successfully restored the backup into a disposable PostgreSQL database, verifying all **15 tables** and **470,767 candle rows** at backup time.
- The user confirmed AgePony successfully decrypted the archive on their Android phone on 2026-11-04.
- Therefore backup creation, disposable restore verification, download, and phone decryption are **complete**. Do not ask the user to repeat them or describe the backup as incomplete.
- The six opportunity-registry tables were not in that 15-table backup because the opportunity migration had not yet been applied. A new post-migration backup is a later maintenance task, not a prerequisite to redo the already successful backup.

### Backup workflow source hardening after the successful backup

- A separate workflow run had exposed that the pinned age archive setup needed the `age-plugin-pq` executable explicitly installed.
- The workflow was hardened to locate, require, install, and version-check `age-plugin-pq` from the same checksum-verified age v1.3.2 archive.
- The hardened workflow was committed to `main` at `c647dc41cf1ca41b2fedf9bb593402aaf5b47615` and to this Part 8 branch at `0f6bee541c367ac83bfdb7bd75d65e391f547c97`; both versions were fetched and verified to have identical content blob SHA `30f0c5e783f3815055cc21b83a4b682c09d347e5`.
- No new backup run was started after this source fix. The previously successful encrypted backup remains valid and user-decrypted.

### Current staging schema and backend-role gate — read-only inspection

- The staging Supabase project `solo-ai-auto-trader-staging` (`gryjtjobkmredawmymwy`) still lists only the six migrations through `20261103063947`; the opportunity-registry migration `20261031170000` is **not applied**.
- A fresh read-only table listing shows the same 15 private `paper_ledger` tables, all with RLS enabled; the six opportunity-registry tables remain absent. The latest listed candle count was 470,317 (a current table listing, distinct from the 470,767 rows in the earlier backup).
- The security advisor returned no lints at the time of this inspection. `anon`, `authenticated`, and `service_role` have no USAGE on the private `paper_ledger` schema; the dedicated `solo_backup_reader` has USAGE and no CREATE.
- The current database activity snapshot did not show an identifiable Render application session. The Supabase connector's own administrative session and platform sessions are not evidence of the Render API's actual database login role. Render's service metadata does not expose the secret connection-string username. Therefore the **actual backend runtime database role remains unverified**; do not infer that it is `postgres`, `service_role`, or the backup reader.
- The Render staging API service remains on the Free plan and is configured to run the API-only command, but it is still attached to the older `feat/solo-ledger-dashboard-integration` branch rather than this Part 8 branch. No deployment or environment change was made.
- Do not apply the opportunity migration until the actual backend runtime role and its required privileges are verified. Production remains untouched.

### Latest source-level CI and Android build evidence

- Part 8 PR #101 head `0f6bee541c367ac83bfdb7bd75d65e391f547c97`: the latest four PR workflows completed successfully (run IDs `41822717827`, `41822717706`, `41822717823`, `41822717619`). The comprehensive durable PostgreSQL job passed preflight unit tests, disposable migration and six-table backup-reader/RLS assertions, worker/API preflight, ledger/runtime/opportunity integration tests, and the complete backend suite. This is disposable-CI verification, not a staging migration or deployment.
- Parts 5–7 PR #97 head `1f4df2c1b5f44a686add9335ba95a292a1f10a6f`: all four latest PR workflows completed successfully (run IDs `41581927605`, `41581927517`, `41581927460`, `41581927526`).
- Integrated notification PR #105 head `f1b235dd4433868f5760966caa73879670ab2785`: Android unit-test task and debug APK build completed successfully in run `41819186986`; root/extracted source mirrors matched. Artifact `solo-staging-market-analysis-debug-apk` (16,594,296 bytes) is available until 2026-11-18. APK SHA-256: `75b850a5d74fa08915278c53421d1238d858a1e8beacc25cc4f90371c7461f74`.
- PR #104's APK workflow was cancelled on its older branch; PR #105 is the integrated notification branch with the successful build.
- No physical Android installation/notification permission/background delivery/restart test has been performed. APK build success is not phone acceptance.

### Remaining acceptance gates

1. Verify the actual Render API (and any worker, if separately run) database login role without exposing credentials; check role attributes, schema usage, and minimum required table privileges. Fail closed if the runtime role is privileged or cannot be established.
2. Keep the opportunity migration unapplied until gate 1 and migration/rollback review are complete. The successful backup above is evidence for the existing 15-table schema; it does not include the six new tables.
3. After an explicitly authorized staging migration, run the 21-table read-only preflight, backup-reader checks, and restore/rollback rehearsal against staging. Never touch production.
4. Install the PR #105 APK on the user's Android device and verify in-app alerts, OS permission grant/denial, foreground and background behavior, process restart, acknowledgement versus resolution, and Emergency Stop semantics.
5. Complete real public-market-data soak and host/API/worker recovery acceptance. The current $0 Render Free topology cannot be represented as a guaranteed always-on worker; do not add keep-alive traffic or paid services.
6. Keep all PRs draft/unmerged until these product and operational acceptance gates pass. Preserve `PAPER_ONLY`, `orders_authorized=false`, independent Risk Guardian authority, and no-live-trading restrictions.


## Unified Solo Parts 1–8 integration checkpoint — 2026-11-04

- Created unified integration branch `integration/solo-complete-parts1-8` and draft PR #107 against the existing Part 4 baseline branch `feat/solo-ledger-dashboard-integration`.
- The unified tree preserves the existing Parts 1–4 baseline, Parts 5–7 implementation, notification core and foreground risk refresh, and Part 8 operational recovery work. The Part 8 files were overlaid onto the latest notification branch tree and both source histories were retained in a two-parent merge commit.
- Android navigation now exposes ten core screen groups: Dashboard, AI Agents, Markets/Charts, Opportunities, Positions, Risk Center, Performance, Notifications, Security/Session, and Settings. Theme preference persists locally. Performance fields that lack a complete verified historical series remain explicitly unavailable; no synthetic metrics are shown.
- The AI screen now lists all 15 specialist roles. It explicitly marks them as specified/not running; the agent network and GAT/PPO research policies are not represented as active services. Risk Guardian remains the independent authorization boundary.
- Root and extracted Android source/test mirrors were verified byte-for-byte identical on the integration branch.
- Unified branch CI at head `4900b4fd48d0a8eb6f306351848f6025eb63c275`:
  - Durable PostgreSQL integration, worker/API staging preflight, paper-service recovery, opportunity registry integration, and full backend suite: **618 passed**.
  - ASGI runtime tests: successful.
  - Market-data and analysis safety validation: successful.
  - Android unit tests and staging debug APK build: successful.
  - Extended-timeframe and branding Android build: successful.
- Staging APK artifact: `13359058843`, 16,613,368 bytes, SHA-256 `f38f885e8adfd60106022ed0945ba0dfa9a8cf0c561183def0f29f4d35b8e4ab`, created 2026-11-04 and expires 2026-11-18.
- This is source/CI completion, not phone acceptance or production deployment. No Render deploy, Supabase migration, secret update, billing change, or production change was made.
- The staging opportunity-registry migration remains unapplied because the actual Render API/worker database login role could not be identified from the available read-only runtime session data. The Supabase activity snapshot showed only platform sessions and no identifiable Render application session. Do not apply the migration until the runtime role and minimum privileges are verified.
- Android physical-device notification acceptance, genuine risk-alert acknowledgement/resolution, background delivery, and real hosted market-data/worker soak remain unverified and are deferred to the final acceptance phase. The free hosting topology is not an always-on worker; no paid service or keep-alive workaround is used.
