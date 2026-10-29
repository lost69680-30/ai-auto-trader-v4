# Solo Current Deployment Status — 2026-10-29

**Purpose:** Current, deployment-specific status supplement. This file supersedes conflicting older runtime claims in the historical `PROJECT_COMPLETENESS_AUDIT.md`; it does not erase that audit's historical notes.

## Safety and deployment

- Project remains research and paper-only. `orders_authorized=False`; no live exchange order placement is authorized.
- Render service: `ai-auto-trader-v4-backend`. Latest verified deployment at the time of this report was live on commit `d9d2f2269b2071f8384181f5c09daff50b0156dc`.
- Latest reviewed startup logs report PostgreSQL paper-ledger selection and successful startup integrity checks. Do not repeat older claims that the active deployment uses `/tmp/v4data/paper-ledger.sqlite3` unless a new runtime inspection proves it.
- Latest reviewed market-feed logs report `SPOT_PUBLIC_ANALYSIS_ONLY`, 12 healthy symbols, and `orders_authorized=False`. This confirms public Spot analysis health only; it is not evidence that Futures mark prices, Futures funding, or account-specific funding payments are active.
- Root API health probes may return 401 because the API is bearer-token protected. A 401 from an unauthenticated probe alone is not evidence that the service is down.
- No Render settings, environment variables, secrets, database configuration, or hosting plans were changed as part of this audit.

## WebSocket runtime

- ASGI/WebSocket support is merged and deployed in source.
- The opt-in `PAPER_WEBSOCKET_ENABLED` flag defaults to disabled; the production service remains on the existing WSGI path unless explicitly configured otherwise.
- The PR CI reported 32 focused ASGI/streaming tests and 369 Python regression tests passing. The WebSocket live test is a loopback test in CI, not an end-to-end test against the deployed Render service.
- Do not claim production WebSocket connectivity until a controlled authenticated client test is run against Render and restart/freshness behavior is verified.
- WebSocket messages are intended for validated public market snapshots only; this does not create an order-control channel.

## Historical evaluation

- `.github/workflows/solo-step5-historical-evaluation.yml` fetches public closed hourly Spot candles and saves a JSON artifact. Its evaluation harness uses chronological walk-forward windows, a separate final holdout, and cash/buy-and-hold reference returns.
- The report is research-only and sets `orders_authorized=False`. The final holdout is reported once and must not be used to tune the strategy.
- Important limitations remain: simplified next-open fills and one-bar holds; estimated fees/slippage; no complete spread/market-impact model; no full stop/liquidation/margin simulation; and the buy-and-hold benchmark is simplified. A successful workflow or synthetic test does not demonstrate profitability.
- Before interpreting results, inspect the actual workflow run and artifact, record the dataset window/fingerprint and assumptions, verify costs and point-in-time data handling, and compare net returns, drawdown, volatility, tail losses, turnover, and trade distribution across multiple symbols/periods. Do not alter the frozen protocol after seeing holdout results.


### First real Spot evaluation — 2026-10-29

- GitHub Actions run: https://github.com/lost69680-30/ai-auto-trader-v4/actions/runs/40904747675 (completed successfully); artifact `solo-v4-historical-evaluation-report` was produced.
- Dataset: BTCUSDT hourly Spot, 999 closed candles from 2026-09-18 00:00 UTC through 2026-10-29 14:00 UTC. Evaluation used the frozen 500-bar warmup, three 100-bar walk-forward windows, and a separate 100-bar final holdout.
- **Strategy activity result: zero trades in all three walk-forward windows and zero trades in the untouched holdout.** Net strategy return and drawdown are therefore both 0%; these are not evidence of profitable performance. The diagnostic shows no eligible same-direction consensus candles at the configured score threshold; only a small number of individual assessments qualified.
- Simplified buy-and-hold returns were -0.028921%, -0.457728%, and +1.817147% across the three walk-forward windows; final holdout buy-and-hold was +0.997636%. These figures are historical reference points only and do not include a complete market-impact/spread model.
- Spot funding assumption is now correctly 0 bps per 8 hours; Futures funding must be evaluated separately with Futures-specific data. All report entries retain `orders_authorized=False`.
- Next research task: diagnose strategy eligibility and activity using training/walk-forward data only, add more symbols/periods and realistic risk/cost metrics, and keep the final holdout untouched until the evaluation protocol is frozen. Do not tune on or claim profitability from this single report.

### Multi-symbol activity diagnostic — 2026-10-29

- GitHub Actions run: https://github.com/lost69680-30/ai-auto-trader-v4/actions/runs/40915995893 (completed successfully); artifact `solo-v4-multi-symbol-research-report` was produced.
- Four symbols were evaluated using 999 closed hourly Spot candles each: BTCUSDT, ETHUSDT, BNBUSDT, and SOLUSDT. The three walk-forward folds produced no BTC, ETH, or BNB trades; SOL produced one trade in one fold with a -0.239184% net strategy return for that fold. The separate untouched holdout produced zero trades for all four symbols.
- A new read-only score-threshold sweep measures signal frequency at score thresholds 0, 20, 40, 60, and 80 on walk-forward folds only. In the first fold, threshold 0 showed 42–55 directional consensus candles per symbol; at 20 this fell to 1–15, and at 40 or higher it was zero for BTC, ETH, and BNB, with only two SOL short-consensus candles at 40. At 60 and 80, all four showed zero consensus candles in that fold.
- **Interpretation:** current score scaling and thresholds appear to suppress almost all consensus candidates. This is a signal-activity finding only, not a profitability result. Do not lower the production threshold just to increase activity. Any future calibration must be pre-registered, trained only on training data, evaluated on separate validation folds, and leave the final holdout untouched.

## Still open

1. Verify the authenticated REST API and Android display without exposing the bearer token.
2. Run a controlled live Render WebSocket test before considering any enablement.
3. Confirm scheduled workflow runs and archived evaluation artifacts; GitHub Actions batch jobs are not a persistent background worker.
4. Validate Futures mark-price/funding ingestion separately from Spot analysis, without changing runtime mode until the source and testnet-only configuration are reviewed.
5. Complete database backup/restore, deployment restart, and multi-day paper soak checks before relying on persistence operationally.
6. Improve realistic execution-cost and risk metrics, preserve an untouched holdout, and make no profitability claims without reproducible out-of-sample evidence.

## Cost and safety policy

- Keep the existing Render service and Supabase PostgreSQL database.
- Do not start a paid plan, trial, migration, or service that can incur charges.
- Keep all API credentials in backend secrets; never place them in the Android APK, reports, prompts, or logs.
- Any failed, stale, missing, contradictory, or unverified data/risk check must block paper actions. The AI strategy cannot override the independent risk manager.
