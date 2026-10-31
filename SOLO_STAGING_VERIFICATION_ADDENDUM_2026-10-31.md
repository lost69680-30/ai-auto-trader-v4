# Solo staging verification addendum — 2026-10-31

**Purpose:** Correct the older audit snapshots without rewriting their historical test results. This addendum records the latest staging and Android evidence available on 2026-10-31.

## Verified Android phone acceptance

The user installed the staging APK and directly confirmed:
- Authenticated market WebSocket displays **connected**.
- Live market prices update.
- Candle charts load.
- Order authorization remains disabled.

These are real-device checks, not merely CI results. They do not yet prove token persistence after app restart/navigation, reconnect after a deliberate network interruption, all timeframe ranges, theme behavior, or every screen/error state. Those checks remain open.

The staging APK was built by GitHub Actions in run [41087085836](https://github.com/lost69680-30/ai-auto-trader-v4/actions/runs/41087085836), commit `2a4081018191ddfee1a3fae65280567010a7b80a`, artifact ID `12992002800`, package `com.aiautotrader.v4.staging`. PR [#60](https://github.com/lost69680-30/ai-auto-trader-v4/pull/60) remains open and unmerged pending the remaining phone checks and review.

## Staging runtime evidence

Read-only inspection of the isolated staging Supabase project `gryjtjobkmredawmymwy` and Render service `solo-futures-websocket-staging` at approximately 2026-10-31 02:22 UTC showed:

- Worker heartbeat status: `RUNNING`; heartbeat timestamp `2026-10-31 02:22:10 UTC`.
- Ten market symbols had fresh snapshots within five minutes; all ten were marked `HEALTHY`.
- Latest observed snapshot: `2026-10-31 02:22:34 UTC`.
- Scheduled jobs: 15 succeeded, zero pending, zero failed.
- Snapshot and heartbeat records report `orders_authorized=false`.
- The active market snapshot payload includes last price, candle data, Futures mark price, index price, funding rate, and next funding time.
- The staging `paper_ledger` schema contains the paper ledger, market snapshot/heartbeat, and durable scheduler tables.

Earlier Render logs showed the supervisor starting the API and market-worker child processes after a Free-service idle shutdown. This verifies whole-service cold-start recovery. It does **not** prove independent API-only or worker-only fault recovery in the hosted environment; CI restart regression tests are not a substitute for staging fault injection.

## Important interpretation and remaining limits

- Render Free may idle the service. A connected phone and successful cold-start do not guarantee uninterrupted 24/7 worker operation.
- Independent process fault injection, longer-duration uptime/soak, backup-and-restore, and database migration recovery remain unverified.
- A healthy public market feed is not proof of live exchange order capability. No order execution is authorized or enabled.
- Historical audit documents contain earlier snapshots that predate the PostgreSQL shared-market-data layer and the phone test. Read them as historical records; use this addendum for the 2026-10-31 staging/phone state.
- No production service, production database, credentials, billing plan, or trading authorization was changed.

## Next verification sequence

1. Continue source-integrity and documentation reconciliation.
2. Verify Android token persistence and reconnect after a deliberate network interruption.
3. Complete API/worker recovery testing using only a safe, controlled mechanism; do not add unauthenticated restart controls.
4. Observe longer-duration snapshot freshness and scheduler cycles.
5. Review analysis, research, and paper-risk modules before beginning advanced AI research.
6. Keep PR #60 unmerged until remaining phone acceptance and review are complete.

**Safety invariant:** paper-only/read-only; `orders_authorized=false`. AI/strategy components cannot override the independent risk manager.
