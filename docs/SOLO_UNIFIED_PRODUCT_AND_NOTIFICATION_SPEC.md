# SOLO — The Hidden Sovereign
## Unified Product Experience and Notification Specification

**Status:** Design/engineering specification; implementation and device acceptance are separate gates.  
**Budget:** $0. No paid services, subscriptions, card verification, or billable provisioning.  
**Scope boundary:** This document does not authorize backend, database, secret, hosting, deployment, or trading-mode changes.

## 1. Product promise

Solo is an intelligent market-analysis and paper-trading application. Its interface must make market evidence, system state, risk decisions, and uncertainty understandable. It must never imply guaranteed returns or conceal stale, missing, rejected, or incomplete data.

**Primary slogan:** Trade with Confidence.  
**Supporting phrase:** Intelligence in Every Decision.

## 2. Master identity — The Hidden Sovereign

The sole master emblem is The Hidden Sovereign: a flowing S silhouette with a subtly revealed lion face in negative space, mane integrated into the S, and a restrained crown integrated into the same geometry.

- Use one master geometry across app icon, splash, wordmark lockup, notifications, charts, and marketing.
- Do not create separate lion, shield, crown, or unrelated symbols.
- Small-size variants may simplify detail but must preserve the same recognizable silhouette.
- Dark, light, and monochrome variants change rendering only, not identity.
- The identity should feel intelligent, calm, disciplined, premium, and trustworthy—not aggressive or cartoonish.

## 3. Visual tokens

| Token | Value | Use |
|---|---|---|
| Obsidian Black | #101216 | Dark primary surface |
| Graphite | #1B1E24 | Dark elevated surface |
| Champagne Gold | #C6A66B | Brand and selected emphasis |
| Antique Gold | #927547 | Secondary gold |
| Platinum | #D7D9DC | Dark-theme text/highlights |
| Silk Ivory | #F4F0E8 | Light primary surface |
| Midnight Bronze | #403426 | Warm dark accent |
| Success | #39B980 | Confirmed positive state |
| Warning | #E7B65E | Caution/degraded state |
| Danger | #E36F72 | Critical/blocked state |

Gold is reserved for identity, selected navigation, key highlights, and important actions. Do not use it as a universal decoration. Maintain readable contrast in both themes. Financial values use tabular numerals.

## 4. Experience principles

1. Power without clutter.
2. Intelligence without mystery.
3. Elegance without distraction.
4. Personalization without compromising safety.
5. Transparency without exaggeration.
6. Performance without waste.
7. Safety before execution.

Use restrained glass-like layering for surfaces and tactile depth for controls. Avoid excessive glow, gradients, shadows, and ornamental motion. The “Sovereign Light” motion motif is subtle, optional, reduced-motion aware, and must never delay safety controls or imply an unconfirmed action succeeded.

## 5. Ten-screen application map

Primary navigation: **Home · Markets · AI Center · Portfolio · Settings**. Risk, Opportunity, and Notifications remain reachable from the dashboard and contextual navigation.

### 01 — Dashboard
- Equity and account mode; realized/unrealized P&L only when backed by fresh data.
- Live feed/connection freshness and last update.
- Emergency Stop state and paper-only status visible without scrolling.
- Risk summary, selected opportunities, portfolio snapshot, recent critical alerts.
- Empty, stale, disconnected, and partial-data states must be explicit.

### 02 — Markets and Advanced Charts
- Symbol and market selection; multi-timeframe charts including 5m, 15m, 1h, 4h, 1d, 1w, 1M, and 1y views where data coverage permits.
- Validated closed candles only; do not fabricate missing candles.
- Indicator overlays/panels, volume, market regime, data freshness, and source attribution.
- Chart rendering is presentation-only and has no order/execution capability.

### 03 — AI Intelligence Center
- Explainable market analysis, multi-timeframe alignment/conflict, indicator evidence, market regime, confidence with calibration context, and explicit no-trade reasoning.
- Specialized analysis workers may contribute evidence but cannot authorize execution.
- Distinguish observed facts, derived metrics, hypotheses, and model outputs.

### 04 — Continuous Opportunity Engine
- Continuous or scheduled scanning only at the cadence the available host can actually sustain.
- Ranked opportunities with evidence, freshness, invalidation conditions, estimated costs, and risk eligibility.
- Opportunity ranking is not an order instruction; default paper handoff remains disabled unless explicitly enabled in the protected configuration.

### 05 — Portfolio, Positions, and Orders
- Paper equity, balances, simulated positions, order lifecycle, fills, fees, slippage, and reconciliation state.
- Clearly label simulated/paper records.
- Uncertain position/order state fails closed and halts new paper intents until reconciled.

### 06 — Risk Center
- Independent Risk Guardian status, exposure, per-trade/daily/portfolio limits, concentration, drawdown, and blocked proposals.
- Risk controls are read-only from AI-generated settings; users/agents cannot weaken protected limits through generated configuration.
- Emergency Stop blocks new orders/intents; do not imply it automatically closes existing positions.

### 07 — Performance and Analytics
- Equity curve, realized/unrealized results, drawdown, win/loss statistics, expectancy, fees, slippage, and sample size.
- Include date range, data coverage, benchmark, and calculation notes.
- Never present empty or incomplete data as zero performance or a verified result.

### 08 — Notifications and Alerts
- Unified in-app alert center, transient toast/banner for timely events, severity, acknowledgement, history, filtering, and notification preferences.
- Critical alerts remain visible until acknowledged and resolved; acknowledgement does not resolve an underlying condition.
- OS-level push/local notifications require runtime permission where applicable; in-app delivery remains available when permission is denied.
- No alert can authorize an order, disable protection, or override Emergency Stop.

### 09 — Authentication and Security
- Login, registration, reset-email flow, session status, sign-out, and security guidance.
- No API secrets in the APK, UI logs, notifications, prompts, or source repository.
- Tokens are stored/handled only according to the existing authenticated backend design.

### 10 — Settings and Preferences
- Dark/light theme, display preferences, alert preferences, chart defaults, and accessibility.
- Show protected settings as locked/read-only with a clear explanation.
- Do not expose hidden execution switches or live-trading toggles.

## 6. Notification event model

Notification pipeline:

`System Event → Validation → Severity → Deduplication → Cooldown → Delivery → Acknowledgement → Resolution`

Every event must carry a stable event ID, event type, severity, concise title/body, event timestamp, source component, and a safe navigation destination. Do not put credentials, access tokens, private keys, or sensitive payloads in notification content.

### Initial event catalogue

| Event key | Severity | Trigger / expected copy |
|---|---|---|
| `EMERGENCY_STOP` | Critical | Backend confirms Emergency Stop is active; say new orders are blocked |
| `TRADING_PAUSED` | High | A verified runtime/risk state pauses new paper intents; explain the reason |
| `DATABASE_FAILURE` | Critical | Durable ledger/database health check fails; explain that state may be unavailable |
| `MARKET_DATA_STALE` | High | Freshness threshold for the selected feed is exceeded; identify affected scope |
| `RISK_LIMIT_BREACH` | Critical | Risk Guardian reports a breached configured limit and blocks the proposal |

Only emit an event after the underlying state is confirmed. Do not synthesize a critical event from an unknown/null status. Recovery events should be emitted only after a fresh successful health/state observation.

### Severity and delivery policy

- **Critical:** persistent in-app alert; request OS notification permission and deliver a local notification when permitted; no silent dismissal; acknowledgement required.
- **High:** prominent in-app alert/banner; optional OS notification according to user preferences.
- **Warning:** in-app feed and non-blocking toast.
- **Info:** history/feed only unless user opts in.

Deduplication uses stable event identity. Cooldown groups repeated instances by event type and scope, but must not suppress a materially changed critical state. Maintain a count/last-seen timestamp when repeated events are coalesced. Do not discard unacknowledged critical events during temporary network loss; persist/replay only through an authenticated, bounded, idempotent event source. If no durable event source exists, do not claim offline delivery guarantees.

### Lifecycle

`CREATED → DELIVERED → ACKNOWLEDGED → RESOLVED`

- `DELIVERED` means the chosen channel accepted the event; it does not mean the user saw it.
- `ACKNOWLEDGED` records that the user acknowledged the alert; it does not clear the condition.
- `RESOLVED` requires a fresh authoritative observation that the source condition is no longer active.
- Preserve timestamps, event ID, severity, source, dedupe count, and lifecycle transitions in the audit trail where supported.

### Failure handling

- No notification may claim a trade/order was placed, filled, or closed without authoritative confirmation.
- If the backend/network is unavailable, display connection uncertainty and the last successful observation time; do not infer current safety state.
- Do not report database failure unless a health check actually failed.
- Do not mark stale market data as fresh merely because the app reconnected.
- Permission denial must not disable the in-app alert center.
- Notification delivery failure must not change risk authorization or runtime state.

## 7. Safety invariants

These requirements apply to every screen, notification, integration, and future workflow:

- `PAPER_ONLY` remains mandatory.
- `orders_authorized=False` remains mandatory.
- The independent Risk Manager/Risk Guardian is the exclusive authorization authority.
- AI, agents, RL, GAT, strategy services, and chart components cannot bypass risk checks or call an order endpoint directly.
- Generated settings cannot weaken protected limits.
- Emergency Stop blocks new orders/intents; it is not represented as automatic liquidation.
- No live trading.
- Credentials remain in backend secret storage and never enter the APK, source code, logs, prompts, or notification payloads.
- If exchange/ledger/position state is uncertain, fail closed and halt new intents.

## 8. Accessibility and interaction

- Minimum comfortable touch targets; clear focus and screen-reader labels.
- Never communicate severity by color alone; use iconography and text labels.
- Respect Android font scaling and reduced-motion preferences.
- Support dark and light themes with verified contrast.
- Toasts must not obscure Emergency Stop, primary navigation, or critical values.
- Critical alerts must remain accessible from the notification center after a toast disappears.

## 9. Verification gates

A feature is not complete merely because a screen or notification is visible.

### Notification tests
- Each of the five event types is emitted only from a confirmed source state.
- Severity, title, body, timestamp, and destination are correct.
- Stable-ID duplicates are suppressed.
- Cooldown coalesces repeats without hiding changed critical conditions.
- Acknowledgement is recorded independently from resolution.
- Resolution requires authoritative recovery.
- Network interruption/recovery preserves the truth of state and does not create false success.
- Permission granted/denied paths both work; in-app feed remains functional.
- No credentials or sensitive data appear in notifications/logs.
- All tests preserve paper-only and disabled order authorization.

### Android acceptance
- Test on a physical Android device for notification permission, foreground/background behavior, channel settings, vibration/sound preferences, deep-link destination, theme, font scaling, and process restart.
- Record OS version, app build SHA, test event, expected result, actual result, and evidence.
- Do not claim phone acceptance from CI or an APK build alone.

### Product-wide acceptance
- Validate all ten screens in dark/light themes.
- Validate loading, empty, stale, disconnected, error, and recovery states.
- Verify the same master emblem geometry is used everywhere.
- Confirm no UI path or notification changes risk authority.

## 10. Release and operating boundary

This specification is documentation only. It does not alter production services, Supabase schemas, credentials, Render configuration, GitHub secrets, or trading permissions. Keep work on a feature branch and use pull-request review. Do not merge or deploy until code tests, security checks, backup/rollback gates, and required physical-device acceptance have passed.

The current strict $0 constraint supersedes any prior paid-host recommendation. Existing free hosting may sleep or delay scheduled work; do not disguise those limits with keep-alive traffic or imply guaranteed continuous execution.
