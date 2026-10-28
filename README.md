# AI Auto Trader v4 — Research and Paper-Trading Assembly

This is a **paper-only Python research and simulation baseline**, not a production or live trading bot. It does not place Binance orders. The USDⓈ-M Futures adapter uses public GET market-data endpoints only; exchange credentials and execution endpoints are intentionally absent.

## Research-to-paper path

1. `backend/market_data_adapter.py` retrieves public Futures metadata, market snapshots, klines, server time, and mark/funding context.
2. `backend/market_worker.py` polls an explicit symbol allowlist, checks feed freshness and candle gaps, retries with bounded backoff, and continues after logged poll/consumer errors.
3. `app/engines/research_pipeline.py` cross-checks scanner, opportunity, strategy, and liquidity/structure evidence. Missing or conflicting evidence blocks the candidate.
4. `app/engines/complete_analysis_pipeline.py` connects eligible research candidates to ATR price planning, signal validation, trigger watching, and paper orchestration.
5. `app/core/risk_guardian.py` rejects malformed/non-finite inputs and independently recomputes stop-distance risk before approving a paper intent.
6. `app/engines/paper_sizing.py` calculates risk-based simulated quantities; `app/engines/paper_ledger.py` persists intents, simulated fills, positions, P&L, emergency-stop state, and audit events in SQLite.
7. `app/engines/paper_runtime.py` accepts only fresh, consistent market frames and sends source timestamps to paper protection.
8. `app/engines/protection.py` simulates stop-loss, take-profit, trailing-stop, emergency-stop, and recovery behavior.
9. `app/engines/paper_service_app.py` composes market polling, research, planning, risk, persistence, simulated fills, and protection into a configured local service.

## Safety boundary

- Every report remains `orders_authorized=False`.
- There is no exchange order placement/cancellation adapter, exchange-account access, or live execution path.
- Risk approval is necessary but would not be sufficient for any future execution adapter.
- Public market-data connectivity and strategy profitability must be measured in the actual target runtime; tests alone do not prove either.
- Testnet is the default public-data host for the app and checker. The current official USDⓈ-M Futures Testnet REST base in Binance's documentation is `https://demo-fapi.binance.com` ([Binance Futures general info](https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info)).

## Install and test

Python 3.11 or later is required. From this directory:

```bash
python -m pip install -e '.[test]'
python -m pytest -q
python -m compileall -q app backend
```

## 1. Run the public connectivity check first

The checker uses only `GET /fapi/v1/time`, `GET /fapi/v1/premiumIndex`, and `GET /fapi/v1/klines`. It checks exchange time vs. local clock, mark freshness, and closed-candle availability. It does not read credentials or place orders.

```bash
python -m backend.market_connectivity_check --symbol BTCUSDT
```

The default is Futures Testnet public market data. An optional production public-data probe (still read-only) is:

```bash
python -m backend.market_connectivity_check --production-public-data --symbol BTCUSDT
```

Exit code `0` means all required probes passed. Exit code `2` means connectivity or validation failed; do not start interpreting a live-data soak until the check passes from the same environment where the service will run. A DNS failure in one workspace says nothing about whether the same host can be reached from your phone, cloud host, or other backend.

## 2. Run a deterministic 24-hour synthetic soak

This validates paper-ledger mechanics, simulated fills, protective exits, stale-quote rejection, fees, and SQLite restart recovery. It is **not** connected to live market data and its scripted P&L is not evidence of strategy performance.

```bash
python -m app.engines.paper_soak --hours 24
```

The runner uses a temporary file-backed SQLite database unless `--database PATH` is supplied. An explicitly supplied database must be new or empty; the runner refuses to mix synthetic soak results into an existing paper account.

## 3. Configure the read-only market paper service

Copy `.env.paper.example` to a private local `.env.paper` file, then review every value. Sample amounts and transaction-cost assumptions are illustrative only. `.env.paper` is ignored by Git.

```bash
cp .env.paper.example .env.paper
# Review/edit .env.paper first.
set -a
. ./.env.paper
set +a
python -m app.engines.paper_service_app
```

`PAPER_MARKET_TESTNET=true` selects public Testnet data; changing it to `false` reads production public market data but does not enable orders. The service has no API-key configuration because it uses no signed or order endpoints. Stop with Ctrl+C or a termination signal. The SQLite ledger is durable local state, so do not delete or copy it while the service runs.

Before treating this as an operational soak, run the connectivity check from the target host, then observe the service over multiple days. Track uptime, stale/missing feeds, rate-limit errors, rejected signals, fills, fees/slippage, daily and peak-to-trough drawdown, exits, emergency-stop drills, and crash/restart behavior. Active signal-watch state is process-local and is not resumed automatically after restart; **the service now durably cancels any pending paper intents on startup** because their trigger/price-plan state cannot be trusted after a restart. Fresh market data and a newly validated signal are required to create a new intent. Open positions remain persisted and still require restart/fresh-mark verification in the target runtime.

## Important modelling limits

- Contract tick size, quantity step, and minimum quantity are required; missing metadata is not guessed. A complete exchange quantity/notional filter model is still needed.
- Funding rates are research context but funding cash flows are not fully included in simulated account P&L.
- Leverage, margin mode, maintenance margin, liquidation, contract multipliers, partial fills, queue priority, and market impact are not comprehensively modelled.
- Strategy performance requires walk-forward and untouched out-of-sample evaluation with realistic costs; the synthetic soak is a reliability test only.
- PPO/RL, GAT, CTDE, MCP/A2A and a production mobile/backend product are not yet implemented/validated here. No Android APK or remote GitHub update is included in this assembly.

See `docs/PAPER_SERVICE.md` and `PROJECT_COMPLETENESS_AUDIT.md` for more details and remaining work.

## Android app shell and cloud APK build

A native Jetpack Compose Android shell now lives in `android/app`. It intentionally shows **Backend not connected** and placeholder values rather than fabricating balances or market data. It has no trading buttons, exchange credentials, or order-placement code. It is a UI milestone, not yet a connected trading client.

The repository includes `.github/workflows/android-apk.yml`. After these files are committed to the GitHub repository, open **Actions → Build Android APK → Run workflow**. When the workflow succeeds, open that run and download the `ai-auto-trader-v4-debug-apk` artifact. Unzip it and install `app-debug.apk` on an Android device. A debug APK is for testing and is not a Play Store release.

The Android build has not been compiled inside this workspace because an Android SDK/Gradle installation is not available here. The GitHub Actions build must pass before treating the APK as verified. The next Android milestone is a read-only authenticated backend API and connection-status screen; only after that should portfolio and position data be wired into the UI.

## Authenticated read-only API (paper mode only)

The optional WSGI API in `app.engines.read_only_api` exposes only `GET` routes:

- `/api/v1/status` — paper-only mode, emergency-stop state and explicit feed status
- `/api/v1/positions` — durable paper positions and P&L
- `/api/v1/orders` — simulated order history
- `/api/v1/risk` — emergency stop, open-position count and latest equity snapshot
- `/api/v1/audit` — audit events
- `/api/v1/funding` — funding events recorded in the paper ledger
- `/api/v1/markets` — returns `UNAVAILABLE` until market-worker state is wired in; no fabricated prices

Set `PAPER_API_TOKEN` to a randomly generated secret of at least 32 characters and
`PAPER_DATABASE_PATH` to the durable SQLite file. Start with
`python -m app.engines.read_only_api`. It binds to loopback by default. For remote
access, put it behind a correctly configured HTTPS reverse proxy and set
`PAPER_API_HTTPS_TERMINATED=1`; do not expose plain HTTP to the internet. There are
no POST/PUT/PATCH/DELETE routes and no exchange order-placement code in this API.

The Android app's read-only connection card accepts the API token in memory for the
current screen session. Set Gradle property `paperApiBaseUrl` to the deployed HTTPS
base URL (or GitHub Actions repository variable `PAPER_API_BASE_URL` for CI builds).
Do not put exchange keys in the APK or use an exchange API key as this API token.
The UI connection is source-implemented but is not build-verified in this environment.

## Market-data outage policy

When market data is unavailable or stale, new paper entries are blocked and durable
open positions are retained with stale/unverified status. Price-triggered paper
stop-loss/take-profit/trailing decisions are not evaluated against an invented or
stale price; they resume only after a fresh, valid mark arrives. This is a simulation
policy, not exchange-side protection, and it must not be interpreted as live account
safety. The API reports feed status as unknown/unavailable until the market worker is
connected to it.

Public funding-rate history can be queried for rate/schedule cross-checks. That public
endpoint is not the account-specific funding cash-flow history, so actual payment
reconciliation remains incomplete. The existing ledger only records supplied funding
events; do not claim exchange-account reconciliation is complete.
