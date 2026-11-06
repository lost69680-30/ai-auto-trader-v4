# Solo self-healing reliability watchdog

## Scope

Strengthens the existing Render supervisor and PostgreSQL worker lease. It does not add a competing worker, scheduler, monitoring vendor, database migration, or order-capable component.

## Signals monitored

- API and worker child-process exit status.
- PostgreSQL worker heartbeat age and lease expiry.
- Market-feed freshness.
- Durable scheduled-job runner progress through last_poll_started_at and last_poll_at.
- Worker process ID/start time and market-cycle timing in heartbeat diagnostics.
- Scheduler error and last dispatch status, separated from scheduler thread progress.

## Recovery rules

1. A confirmed stale/missing/stopped worker heartbeat after startup grace triggers termination and supervised restart of the worker child.
2. A scheduler thread whose most recent poll start/completion timestamp is older than the configured threshold triggers a worker-only restart even if the market worker continues publishing fresh heartbeats.
3. A failed individual research job does not trigger a process restart when scheduler progress continues advancing; durable scheduler retry/lease recovery remains responsible.
4. A transient PostgreSQL health-read exception is not proof of worker failure. The supervisor preserves the process and does not reset restart backoff.
5. Restart backoff resets only after continuously verified health for the stable period; process uptime alone is insufficient.
6. Missing or malformed scheduler diagnostics do not cause false-positive restarts. Legacy heartbeat rows remain compatible.

## Defaults and bounds

| Setting | Default | Allowed range |
|---|---:|---:|
| PAPER_SUPERVISOR_SCHEDULER_STALE_SECONDS | 900 seconds | 60–3600 |
| PAPER_SUPERVISOR_RESTART_STABLE_SECONDS | 300 seconds | Minimum 60 |
| Existing worker startup grace | 180 seconds | Existing behavior |
| Existing worker health-check interval | 5 seconds | Existing behavior |

The 15-minute threshold is intentionally much longer than the normal five-second poll interval. It avoids treating brief slow jobs as a dead thread while still recovering a runner that has stopped progressing.

## Safety

- No exchange order interface or credential is introduced.
- PAPER_ONLY remains mandatory and every heartbeat/status report carries orders_authorized=false.
- Emergency Stop is never automatically cleared.
- AI, research, strategy, and scheduler components gain no order authority.
- Database uncertainty remains fail-closed; the supervisor does not assume a second worker is safe.

## Operational acceptance still required

CI uses disposable PostgreSQL and does not prove a live Render restart, uninterrupted Render Free uptime, Android background notification delivery, or production database recovery. Those require staging and physical-device checks. Render Free sleep and GitHub schedule delays remain hosting limitations.
