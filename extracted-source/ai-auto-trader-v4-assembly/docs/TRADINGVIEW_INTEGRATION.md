# Solo TradingView Integration

## Scope and product decision

Solo uses TradingView Lightweight Charts as its Android candlestick renderer. The chart library is presentation-only: it does not supply market data, calculate Solo's risk limits, create paper intents, or expose an order interface. Solo remains the source of truth for market data and all analysis.

Use the official Android wrapper and the published `com.tradingview:lightweightcharts:5.2.1` artifact. The wrapper renders the chart in Android WebView. Keep the existing native Compose dashboard, symbol selector, timeframe selector, and Solo analysis panels.

Do not embed TradingView Advanced Charts or Trading Platform in this repository. Advanced Charts is distributed through a private, approval-based repository and is not redistributable in public repositories. Lightweight Charts is Apache-2.0 and suitable for this project, subject to its attribution requirement.

## Implemented in this branch

- Adds TradingView Lightweight Charts Android wrapper.
- Replaces the hand-drawn Compose Canvas candlestick renderer with the official TradingView renderer.
- Continues to source OHLC candles from Solo's authenticated `GET /api/v1/markets/candles?symbol=...` endpoint. No TradingView market-data entitlement or external price source is assumed.
- Preserves the existing validated-closed-candle and timeframe selection path.
- Converts ISO-8601 candle open times to UTC epoch seconds and rejects malformed timestamps, non-finite or inconsistent OHLC values, negative volume, non-monotonic timestamps, and values that cannot be represented safely by the chart API.
- Keeps chart errors isolated from the paper ledger and risk state.
- Supports Solo dark/light chart backgrounds.
- Retains TradingView's chart attribution logo through the chart option `attributionLogo = true`; the UI must be verified on-device to confirm the linked credit is visible in both themes.

## Data and analysis boundary

1. Solo backend supplies closed OHLCV candles and explicit feed/interval status.
2. Android validates the existing candle history and passes only the selected interval to the chart renderer.
3. TradingView Lightweight Charts draws the supplied candles; it is not treated as an independent data provider.
4. Solo Indicator Intelligence, multi-timeframe assessment, candle-pattern analysis, strategy research, and paper ledger remain independent of the chart component.
5. A chart-rendering failure must never change market-feed health, create a signal, approve risk, or alter emergency-stop state.

## Timeframes

The chart uses the timeframe list already supported by Solo's backend and UI. The currently wired chart path must not claim a timeframe is available unless its backend interval status is healthy and the candle series passes validation. Yearly bars must continue to be derived from complete monthly history by Solo, not fabricated by the renderer.

## TradingView alerts and webhooks — gated follow-on

TradingView alerts are a separate signal-ingestion integration and are not enabled by this chart renderer. Before an alert endpoint is exposed, the hosting edge must demonstrate that it can authenticate TradingView's documented HTTPS client-certificate identity. Do not place API tokens, passwords, or shared credentials in TradingView alert URLs or message bodies. Do not trust an unverified forwarding header.

When the edge-authentication requirement is met, the webhook gateway may accept a narrowly defined alert event, validate schema/symbol/timeframe/timestamp, apply replay protection and rate limits, persist an immutable provenance record, and enqueue it as research evidence only. It must acknowledge quickly and process asynchronously because TradingView cancels webhook requests that take longer than three seconds. Failed, duplicated, stale, unauthenticated, or malformed alerts are rejected. An alert is never an order or risk approval.

Until that gate is verified, users may use TradingView's own app notifications or alert log; Solo must not advertise webhook ingestion as active.

## Safety invariants

- `PAPER_ONLY` remains mandatory.
- `orders_authorized=False` remains mandatory in chart/API/research records.
- No TradingView component, alert, Pine Script, external strategy, AI agent, or Android UI can call an exchange order endpoint.
- Risk Guardian remains independent and exclusively controls paper-intent approval.
- Missing, stale, malformed, gapped, or contradictory market data blocks analysis handoff.
- Emergency Stop continues to block new paper intents independently of the chart.
- No live trading, live order placement, order cancellation, or paid TradingView subscription is introduced.

## Acceptance tests

- Kotlin unit tests prove valid timestamp conversion and reject malformed timestamps, out-of-order candles, invalid OHLC envelopes, and non-finite values.
- Android unit tests and debug APK build must pass in GitHub Actions.
- On-device acceptance must verify chart load, symbol changes, timeframe changes, real-time candle refresh, dark/light theme, chart attribution, and behavior when the backend returns no data or an unavailable interval.
- Confirm the chart never shows retained/stale candles as live and never fabricates bars.
- Confirm all existing Part 5–7 backend, risk, paper-ledger, and CI checks still pass.

## Official references

- TradingView Lightweight Charts: https://www.tradingview.com/lightweight-charts/
- Android wrapper: https://github.com/tradingview/lightweight-charts-android
- Android wrapper documentation: https://tradingview.github.io/lightweight-charts/docs/next/android
- Webhook configuration and security guidance: https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/
- Advanced Charts access and redistribution restrictions: https://www.tradingview.com/charting-library-docs/latest/quick-start/