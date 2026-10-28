# Continuous paper runtime

`app/engines/paper_runtime.py` is a paper-only runtime wrapper around the public market worker, an injected analysis handler, the durable SQLite ledger, and the paper protection engine. It contains no exchange order API.

## Runtime behavior and rejection rules

- A poll timestamp must be timezone-aware, not future-dated, and within the configured maximum age.
- A healthy frame must match the poll timestamp, carry a canonical unique symbol, retain `orders_authorized=False`, and contain a positive finite mark with a fresh, non-future observation timestamp.
- Candle evidence must consist of actual `MarketCandle` records with finite, internally consistent OHLCV values, ordered timestamps, and closed candles. Any duplicate symbol blocks all copies of that symbol to avoid ambiguous price selection.
- Only validated frames reach the injected analysis handler. Unhealthy-only polls cannot produce fill or protection prices.
- The runtime snapshots pending intents before analysis. Intents created during the current analysis cycle cannot be filled by that same poll.
- A pending paper intent is not filled with a quote whose observation timestamp predates the intent. Simulated fill time is the runtime processing time; this is still a simplified fill model, not a latency/slippage simulator.
- Position protection receives each quote's original observation timestamp instead of relabeling an older quote as the current time. A stale or out-of-order mark is rejected by the ledger/protection layer.
- Emergency stop blocks paper fills. Price updates are still required to simulate closing positions at supplied market prices.
- The market worker logs poll-consumer or poll-cycle exceptions and continues with bounded backoff. Operations must monitor these logs; exception recovery is not a substitute for health metrics and alerts.
- Reports always set `orders_authorized=False`.

## Configuration still required

Callers must provide a correctly configured `MarketDataWorker`, trusted analysis handler, verified contract tick/quantity rules, transaction cost assumptions, simulated equity, daily P&L, open-position state, and risk limits. This package does not infer those values.

## Run locally

```bash
python -m pip install -e '.[test]'
python -m pytest -q
```

Use a file-backed SQLite path for restart recovery. This remains a local paper runtime, not a deployed always-on backend, Binance Testnet executor, or live trading system.
