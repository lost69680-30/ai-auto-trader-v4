# Configured paper-trading service

This milestone adds a concrete composition layer for the existing research and paper-only engines. It is not an exchange execution system. The service uses public market-data GET requests only and exposes no order placement, account, leverage, or API-secret path.

## Pipeline

1. `MarketDataWorker` polls the allowlisted symbols for marks, funding context, and closed candles.
2. `PaperRuntime` rejects unhealthy, stale, mixed, duplicate, or malformed frames.
3. `PaperServiceAnalysisHandler` fetches a current public futures universe snapshot, checks symbol eligibility and mark/last-price divergence, and refuses symbols without exchange tick size, lot step, and minimum quantity metadata.
4. `analyze_research_universe` runs scanner, opportunity, strategy, and liquidity/structure analysis. It does not assume a direction when evidence is missing.
5. `CompleteAnalysisPipeline` plans prices with ATR, validates the strategy handoff, watches the trigger, sizes quantity to the configured risk budget and contract quantity step, and passes the request to the independent risk gate.
6. `SQLitePaperLedger` records only paper intents. Subsequent fresh poll frames can produce simulated fills, P&L marks, and stop-loss/take-profit/trailing-stop protection.

All report objects retain `orders_authorized=False`. A missing or ambiguous exchange filter, stale/unavailable universe, invalid price, cost assumption above the configured cap, or risk-limit failure blocks that symbol.

## Install and test

Python 3.11 or later is required.

```bash
python -m pip install -e '.[test]'
pytest -q
```

If build dependencies cannot be downloaded in your environment, use an environment with the project test dependencies installed; a successful syntax check alone is not a substitute for the suite.

## Configure a local paper run

Copy `.env.paper.example` to a private local `.env.paper` file and review every value. The sample numbers are illustrative, not venue fee advice or recommended trading limits. `PAPER_ESTIMATED_ROUND_TRIP_COST_PCT` is expressed in **percent units** (`0.12` means 0.12%), while `PAPER_FEE_RATE` is a decimal fraction (`0.0004` means 0.04% per fill) and `PAPER_SLIPPAGE_BPS` is basis points per simulated fill.

Load the variables in a shell, then start the service:

```bash
set -a
. ./.env.paper
set +a
python -m app.engines.paper_service_app
```

By default, `PAPER_MARKET_TESTNET=true` uses Binance’s current documented USDⓈ-M Futures Testnet public REST host, `https://demo-fapi.binance.com` (see [Binance Futures general info](https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info)). Set it to `false` only to read public production market data; this does not enable orders. There are no API-key settings in this example because the service has no signed endpoint or order API.

The SQLite file is durable local state. Do not delete or copy it while the process is running. A production deployment still needs secure persistent storage, process supervision, authenticated control endpoints, health alerts, backups, and explicit operational procedures.

## Risk/accounting boundaries

- Contract `PRICE_FILTER.tickSize` and `LOT_SIZE.stepSize`/`minQty` come from the exchange metadata. Missing or duplicate filter types are not guessed.
- A conservative aggregate gross-exposure cap defaults to simulated equity. There is no margin/leverage/liquidation model, so leveraged risk must not be inferred from these simulations.
- Fee and slippage inputs are estimates. Funding is used as research context, but funding cash flows are not yet added to simulated account P&L.
- Closed-candle strategy data and marks have different purposes; the service checks freshness and a configured mark/last-price divergence threshold.
- Watch state is process-local; the SQLite ledger persists paper orders/fills/positions, but active signal watches are not restored on restart. After restarting, new signal watches must be rebuilt from fresh market data.
- Public REST-only connectivity from a given deployment is not guaranteed by local tests. Verify network access and feed-health logs before any soak test.

## Connectivity preflight

Run this command **from the same machine/container where the service will run** before a fresh-market-data soak:

```bash
python -m backend.market_connectivity_check --symbol BTCUSDT
```

By default it uses Testnet public GET endpoints only: `/fapi/v1/time`, `/fapi/v1/premiumIndex`, and `/fapi/v1/klines`. It reports endpoint latency, local/exchange clock skew, mark-price freshness, and closed-candle availability. Exit code `0` means all checks passed; exit code `2` means the connection or validation failed. No order request or API credential is used. A production-public-data diagnostic is available with `--production-public-data`; it is still read-only.

## Synthetic soak vs. live-data soak

Run a repeatable 24-hour **synthetic** recovery/ledger soak with:

```bash
python -m app.engines.paper_soak --hours 24
```

This deliberately scripts price paths to check 288 paper intents/fills/exits, hourly SQLite reconnections, stale quote rejection, fees, and drawdown accounting. Its P&L is generated by the scripted paths and is not a backtest or performance evidence. It is kept separate from a live-data paper soak.

After the connectivity check passes on the target host, run the configured `python -m app.engines.paper_service_app` against fresh public data and inspect `paper_orders`, `paper_fills`, `paper_positions`, and `paper_audit_events`. Track uptime, stale/missing frames, rate-limit and retry errors, rejects, simulated fees/slippage, daily and peak-to-trough drawdown, SL/TP/trailing exits, emergency stop, and crash/restart recovery. Active watches are process-local and are not restored on restart. Compare actual market-data results with untouched out-of-sample backtests. No claim of profitability follows from the synthetic soak or unit tests.
