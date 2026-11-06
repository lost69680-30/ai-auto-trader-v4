# Solo self-healing reliability watchdog

## Scope

This change strengthens the existing Render supervisor and PostgreSQL worker lease. It does not add a second worker, scheduler, monitoring vendor, database migration, or order-capable component.

## Signals monitored

- Child-process exit status for the API and market worker.
- PostgreSQL worker heartbeat age and lease expiry.
- Market-feed freshness as evaluated by the shared market store.
- Durable scheduled-job runner progress (last_poll_started_at and last_poll_at).
- Worker process ID/start time and market-cycle timing in heartbeat diagnostics.
- Scheduler error and last dispatch status, kept separate from scheduler thread progress.

## Recovery rules

1. A confirmed stale/missing/stopped worker heartbeat after the startup grace triggers worker process termination. The supervisor restarts that child with bounded exponential backoff.
2. A scheduler thread whose most recent poll start/completion timestamp is older than the configured threshold triggers the same supervised worker restart, even if the main market worker continues publishing fresh heartbeats.
3. A failed individual research job does not trigger a process restart when scheduler progress continues advancing. The durable scheduler remains responsible for retry/lease recovery.
4. A transient PostgreSQL health-read exception is not proof of worker failure. The supervisor preserves the process and does not reset its restart backoff.
5. Restart backoff resets only after continuously verified worker health for the configured stable period; process uptime alone is not sufficient evidence.
6. Missing or malformed scheduler diagnostics do not cause false-positive restarts. Legacy heartbeat rows remain compatible until the worker publishes the new progress fields.

## Defaults and bounds

| Setting | Default | Allowed range |
|---|---:|---:|
| PAPER_SUPERVISOR_SCHEDULER_STALE_SECONDS | 900 seconds | 60–3600 |
| PAPER_SUPERVISOR_RESTART_STABLE_SECONDS | 300 seconds | Minimum 60 |
| Existing worker startup grace | 180 seconds | Existing behavior |
| Existing worker health-check interval | 5 seconds | Existing behavior |

The 15-minute scheduler threshold is intentionally much longer than the normal five-second poll interval. A long-running handler can therefore finish without being mistaken for an immediate thread failure; a handler that makes no progress beyond the configured bound is recovered by restarting the worker. The durable scheduler lease/idempotency rules remain the authority for reclaiming unfinished work.

## Safety

- No exchange order interface or credential is introduced.
- The worker and supervisor remain read-only with respect to exchange APIs.
- Every heartbeat/status report continues to carry orders_authorized=false.
- PAPER_ONLY remains mandatory.
- Emergency Stop is never automatically cleared.
- Worker restart does not grant authority to AI, research, strategy, or scheduler components.
- If the database is unavailable or worker ownership is uncertain, readiness remains fail-closed; the supervisor does not assume a second owner is safe.

## Operational acceptance still required

CI verifies deterministic watchdog decisions and the existing disposable-PostgreSQL recovery suite. It does not prove a live Render process restart, uninterrupted Render Free uptime, Android background notification delivery, or production database recovery. Those require separate staging/physical-device checks. Render Free sleep and best-effort GitHub scheduled execution remain hosting limitations, not failures the in-process supervisor can eliminate.
