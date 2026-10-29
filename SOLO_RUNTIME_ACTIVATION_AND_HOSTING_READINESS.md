# Solo Runtime Activation and Hosting Readiness

**Reviewed:** 2026-10-29  
**Scope:** Read-only operational runbook. This document does not change production settings or enable any runtime feature.

## 1. Verified current state

- Read-only Render inspection confirms production service `ai-auto-trader-v4-backend` remains on the **Free** plan in Oregon, tracks `main`, and has automatic deploy on commit enabled. After PR #41 was merged, production commit `1b93f30677f82a3028a1f85055166da839549cd5` deployed successfully at `2026-10-29T20:58:45Z`.
- The service's configured start command still uses `python -m app.engines.read_only_api` and supplies `PAPER_DATABASE_PATH=/tmp/v4data/paper-ledger.sqlite3` as the local fallback path. Recent startup logs explicitly say the ledger selected PostgreSQL and passed integrity checks; this is consistent with `PAPER_DATABASE_URL` taking precedence. No database setting was changed. The Render service summary did not expose environment-variable values, and no secret values were retrieved or printed.
- The latest available feed log explicitly says `market data mode=SPOT_PUBLIC_ANALYSIS_ONLY; spot last price is not futures mark price`. Earlier inspected feed logs reported 12 healthy symbols and `orders_authorized=False`. This confirms Spot public-data health only.
- The research-to-paper worker cannot be enabled while market mode is Spot: startup code exits if `PAPER_RESEARCH_WORKER_ENABLED=true` and `PAPER_MARKET_DATA_MODE` is not `futures`. Given the observed Spot startup, the research worker is not active on this deployed configuration.
- `PAPER_WEBSOCKET_ENABLED` defaults to false. The current Render environment flag could not be read from the service metadata, and the targeted WebSocket log query returned a temporary Render log-service error, so production WebSocket enablement remains **unverified**, not assumed false.
- PR #41 is now merged into `main` as `1b93f30677f82a3028a1f85055166da839549cd5`: https://github.com/lost69680-30/ai-auto-trader-v4/pull/41. The post-merge GitHub Actions run passed backend/Android contract tests, package parity, Python compilation, Android unit tests, and the Solo debug APK build/upload. Production startup logs still report Spot public market data, PostgreSQL ledger selected with integrity checks passed, 12 healthy symbols, and `orders_authorized=False`. No production market-mode or WebSocket flags were changed.

A healthy public Spot feed is not proof that Futures funding, paper analysis, WebSockets, scheduled reports, or restart recovery are active. No production service settings, environment variables, secrets, or deployment actions were changed by this inspection.

## 2. Required runtime configuration — staging only

Do not paste secret values into GitHub, this document, APKs, test artifacts, or chat. Configure credentials only in the hosting provider's secret manager.

### Public market feed and flags

| Variable | Required staging setting / rule |
|---|---|
| `PAPER_MARKET_DATA_MODE` | `futures` for the Futures analysis worker |
| `PAPER_MARKET_TESTNET` | Explicit `true` for the first Futures Testnet smoke test. `false` selects public production Futures market data only; it does not authorize orders. |
| `PAPER_RESEARCH_WORKER_ENABLED` | `true` only after every required paper-risk assumption is supplied and validated |
| `PAPER_WEBSOCKET_ENABLED` | `true` only in staging for authenticated streaming smoke tests |
| `PAPER_MARKET_SYMBOLS` and `PAPER_SYMBOLS` | Must match exactly; use an allowlist of 1–20 symbols supported by the selected Futures venue |
| `PAPER_MARKET_INTERVAL` and `PAPER_CANDLE_INTERVAL` | Must match exactly |
| `PAPER_MARKET_CANDLE_LIMIT` and `PAPER_CANDLE_LIMIT` | Must match exactly; paper candle limit must be 30–500 |
| `PAPER_MARKET_POLL_INTERVAL_SECONDS` and `PAPER_POLL_INTERVAL_SECONDS` | Must match exactly. The current API default is 15 seconds while the paper worker default is 5 seconds, so relying on defaults causes a startup mismatch. |
| `PAPER_MAX_MARKET_AGE_SECONDS` | Must be the chosen common freshness bound; the worker and API use the same value. |
| `PAPER_API_TOKEN` | Keep the existing or separately generated staging token in the secret manager only; require at least 32 characters. Never print it. |
| `PAPER_DATABASE_URL` | Use a **staging-only** PostgreSQL URL for staging. The process deliberately fails closed if this variable exists but is empty. Do not point staging at the user's production ledger. |

The first staging run should use Futures Testnet public market data. Do not switch to production public Futures data until the explicit data-source choice is reviewed. The adapter is public-read-only and has no exchange order-placement interface.

### Required explicit paper-risk assumptions

If `PAPER_RESEARCH_WORKER_ENABLED=true`, provide all of the following; the application intentionally does not guess them:

- `PAPER_SIMULATED_EQUITY`
- `PAPER_RISK_FRACTION`
- `PAPER_MAX_POSITION_NOTIONAL`
- `PAPER_MAX_DAILY_LOSS`
- `PAPER_MAX_OPEN_POSITIONS`
- `PAPER_MAX_RISK_PER_TRADE`
- `PAPER_ESTIMATED_ROUND_TRIP_COST_PCT`
- `PAPER_FEE_RATE`
- `PAPER_SLIPPAGE_BPS`

Additional optional settings include `PAPER_MAX_SIGNAL_AGE_SECONDS`, `PAPER_PLAN_LIFETIME_SECONDS`, `PAPER_WATCHER_MAX_AGE_SECONDS`, `PAPER_TRAILING_DISTANCE_PCT`, `PAPER_TRAILING_ACTIVATION_PCT`, and `PAPER_MAX_GROSS_EXPOSURE`.

Validate all values before enabling the worker:

- `PAPER_RISK_FRACTION` must be positive and no more than 2%.
- `PAPER_MAX_DAILY_LOSS` cannot exceed 2% of the declared simulated-equity baseline.
- `PAPER_MAX_POSITION_NOTIONAL` cannot exceed 10% of the simulated-equity baseline.
- Gross exposure cannot exceed simulated equity; the paper model has no leverage model.
- All numeric fees, costs, equity, and risk settings must be finite and within their code-enforced ranges.
- The worker's symbol list, candle interval, candle limit, polling interval, and market-age setting must match the API's values exactly.
- Any missing, stale, malformed, contradictory, or unsafe input must keep the relevant feed/analysis cycle degraded or blocked; never override the independent risk manager.

## 3. Staging smoke-test sequence

Do not use production Render or the production Supabase ledger for the initial test.

1. Run the existing CI suites on the exact candidate commit. The merged PR #41 commit passed its pre-merge suites, and the post-merge `test-and-build` workflow also passed:
   - ASGI focused tests, end-to-end worker-to-WebSocket loopback test, full Python suite, and compile: https://github.com/lost69680-30/ai-auto-trader-v4/actions/runs/40948623694 — passed (37 focused tests, 384 full-suite tests).
   - PostgreSQL integration plus root full backend suite: https://github.com/lost69680-30/ai-auto-trader-v4/actions/runs/40948623747 — passed (6 PostgreSQL integration tests, 385 full-suite tests).
   - Assembly PostgreSQL integration and full backend suite: https://github.com/lost69680-30/ai-auto-trader-v4/actions/runs/40948623819 — passed (385 full-suite tests).
2. The isolated Free Render staging service is now deployed on branch `fix/futures-context-mainline`: https://solo-futures-websocket-staging.onrender.com. The post-deploy logs confirm `FUTURES_TESTNET`, authenticated read-only WebSocket endpoint startup, and completed paper-analysis cycles. This is a partial smoke test, not full release validation.
3. Start with a small, explicitly allowlisted Futures Testnet symbol set. Use a disposable staging ledger/database, not production Supabase. Never reuse the production `PAPER_DATABASE_URL` for tests that can create paper ledger records.
4. Confirm startup logs identify the selected public market source, PostgreSQL (or explicitly isolated test storage), and `orders_authorized=False`. Do not share log lines that might include secrets.
5. Call authenticated `/api/v1/status` without displaying the token. Confirm `analysis_runtime.enabled=true`, then wait for an actual completed cycle before interpreting its state as `RUNNING`. `STARTING`, `DEGRADED`, and `ERROR` are not success.
6. Authenticate to `/ws/v1/markets` with a test client and confirm a fresh event contains `market_type=FUTURES`, `mark_price`, `index_price`, `funding_rate`, and a future `next_funding_at`, plus `orders_authorized=false`. Verify invalid/stale snapshots are dropped and unauthenticated clients are rejected.
7. **Still pending:** connect the isolated staging service to the isolated staging PostgreSQL database, then restart the staging process and verify state persists; verify pending process-local intents are cancelled and not resumed blindly. Confirm no real exchange orders or API-key permissions are used. The current hosted smoke service still uses an ephemeral SQLite path, so do not rely on it for restart recovery.
8. Observe several cycles and review memory, reconnects, data freshness, symbol exclusions, risk rejection reasons, ledger writes, and restart behavior before any production decision.

A loopback WebSocket test in GitHub Actions does not prove that a deployed Render WebSocket is reachable or enabled.

## 4. Scheduled jobs versus continuous workers

- The research-to-paper worker is intended to be a **continuous loop** inside the long-running API process when explicitly enabled; it is not a GitHub Actions cron workflow.
- Bounded tasks such as historical evaluation and periodic reports can remain separate scheduled jobs. A scheduled job must finish and exit; it is not a substitute for a persistent WebSocket endpoint or an always-running worker.
- The free Render web-service sleep behavior can interrupt continuous work: a service sleeps after 15 minutes without an incoming HTTP request or an inbound WebSocket message. Outbound market polling alone does not establish that the service will stay awake. Free service filesystem state is ephemeral, although the current paper ledger is configured to PostgreSQL.
- The API's one-way WebSocket protocol is intended for validated market snapshots only. Do not add client-message handling that can authorize orders merely to keep the host awake.

Official Render details: https://render.com/docs/free and https://render.com/changelog/free-web-services-now-remain-active-while-receiving-websocket-messages

## 5. Hosting alternatives — checked 2026-10-29

| Provider / plan | Published pricing / limit | Suitability for Solo |
|---|---|---|
| Current Render Free | $0; spins down after 15 minutes with no inbound HTTP or WebSocket messages; no persistent disk or one-off jobs. Free filesystem changes are lost on restart/redeploy/spin-down. | Good for a low-cost API preview, not a guaranteed continuously running worker. Supabase Postgres remains external and should remain unchanged. |
| Northflank Developer Sandbox | Pricing page advertises 2 services, 2 jobs, 1 database and up to $20/month of cloud resources with no sleeping. However, the official pricing page says “No card needed” while the billing docs say users must add a payment method before creating resources. Usage is billed by the second. | Candidate for technical **staging** only; do not create anything until the payment-method conflict, billing behavior after included free resources, and production suitability are confirmed in the actual account. |
| Railway Free | $0, with $5 credit for the first 30-day trial and then $1/month resources; up to 0.5 GB RAM per service after trial. Hobby is $5 minimum monthly usage and usage above included credits is billable. | Free plan is too constrained to assume suitability for Solo's always-running analysis plus WebSockets; do not migrate based on the trial allowance. |
| Fly.io shared compute | The updated official price effective October 1, 2026 lists $8.78/month for a 1 GB shared-cpu-4x machine, before storage and egress as applicable. | A low-cost always-on candidate, but not a zero-charge migration; storage, network, operational setup, and billing controls need full review. |

Sources (official provider pages):
- Render free limits: https://render.com/docs/free
- Northflank pricing (currently conflicts with the billing docs on card requirements): https://northflank.com/pricing
- Northflank billing requirements: https://northflank.com/docs/v1/application/billing/pricing-on-northflank
- Northflank WebSocket ports: https://northflank.com/docs/v1/application/network/configure-ports
- Railway pricing: https://railway.com/pricing
- Fly.io updated pricing effective October 1, 2026: https://fly.io/pricing-update/

### Current recommendation

Keep the existing production Render + Supabase + GitHub setup unchanged. The no-charge staging resources now exist:
- Supabase staging project `solo-ai-auto-trader-staging` (`gryjtjobkmredawmymwy`) is ACTIVE_HEALTHY in `eu-central-1`; the private `paper_ledger` schema has been applied and all nine tables have RLS enabled and empty state.
- Render staging service `solo-futures-websocket-staging` is live on `fix/futures-context-mainline`; logs show a healthy Futures Testnet feed for BTCUSDT/ETHUSDT and completed paper cycles, all with `orders_authorized=False`.
- The staging service is still using its ephemeral local SQLite ledger because a safe database connection string/pooler host has not been obtained through the available management tools. The Supabase staging project is not yet connected. External authenticated WebSocket handshake and restart persistence are also unverified.
- A separate Free Render Postgres test instance exists but is unused and expires `2026-11-28T20:54:47Z`; it is not connected to either service. No paid plan was activated and no production DB schema/data/secrets changed.

For the complete target workload, compare staying on Render with a paid non-sleeping service against external hosts only after memory/CPU behavior and the final monthly budget are confirmed. Do not enable the Futures worker or WebSocket in production until the staging database connection, external WebSocket handshake, and restart/persistence tests pass.


## 6. Current rollout record — 2026-10-29

- PR #41 (Spot/Futures market context and WebSocket pacing) is merged as `1b93f30677f82a3028a1f85055166da839549cd5` and Render production is live on that commit.
- Post-merge CI: offline backtest/safety tests passed; read-only market-data/analysis safety tests passed; `test-and-build` passed including Android unit tests and debug APK artifact upload. Run: https://github.com/lost69680-30/ai-auto-trader-v4/actions/runs/40953626320
- Download artifact: https://github.com/lost69680-30/ai-auto-trader-v4/actions/runs/40953626320 (artifact `solo-v4-extended-timeframes-debug-apk`, expires 2026-11-12).
- Production feed mode remains `SPOT_PUBLIC_ANALYSIS_ONLY`; this deploy did not enable production Futures analysis or WebSockets. Logs report 12 healthy public-market symbols, PostgreSQL ledger selected/integrity passed, and `orders_authorized=False`.
- Hosted staging reports Futures Testnet mode and repeated paper-analysis cycles with healthy BTCUSDT/ETHUSDT, but currently warns its ledger is ephemeral. It must not be described as persistence-verified.
