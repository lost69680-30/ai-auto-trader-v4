# Solo independent worker heartbeat monitoring

## What this does

The scheduled monitor runs in GitHub Actions, independently of Render. It connects to Supabase PostgreSQL using the dedicated `solo_backup_reader` role through the `SOLO_BACKUP_DATABASE_URL` GitHub Actions secret.

It performs SELECT-only checks of:
- worker heartbeat freshness and lease validity;
- scheduler status and last successful poll time;
- market-feed freshness and configured symbol coverage;
- worker and feed `orders_authorized=false` invariants.

It reports one of:
- `HEALTHY`: worker heartbeat, lease, scheduler, and market snapshots are fresh and paper-only;
- `DEGRADED`: worker process is alive but scheduler or market-data quality needs attention;
- `STOPPED`: worker status is stopped/error/stale, heartbeat expired, or lease expired;
- `SAFETY_VIOLATION`: any order-authorization invariant is violated;
- `UNKNOWN`: database or heartbeat evidence is unavailable or malformed.

The scheduled workflow keeps the database inspection SELECT-only. It sends one request to the worker's `/healthz` liveness route only after the read-only classifier confirms the worker is `STOPPED` (including stale heartbeat or expired lease). It then polls read-only heartbeat/scheduler state to verify recovery. A degraded feed with a live worker and progressing scheduler does not trigger a wake. Unknown or unsafe authorization state never triggers a wake.

## GitHub setup

The repository needs the existing Actions secret:

- `SOLO_BACKUP_DATABASE_URL`: PostgreSQL connection string for the dedicated read-only `solo_backup_reader` role. The monitor requires TLS and sets the session read-only. Never expose this value in logs, source, or Android configuration.

The monitor workflow is `.github/workflows/solo-render-keepwarm.yml` (historical filename) and runs every ten minutes plus manual dispatch. It is a conditional recovery controller, not a keep-alive: normal healthy/degraded-feed checks do not contact Render. A second manual workflow, `.github/workflows/solo-independent-worker-watchdog.yml`, validates and runs the read-only classifier on demand.

## Render Free limitation

Render Free web services may stop after inactivity. When the read-only heartbeat confirms that the worker has stopped, the monitor makes at most one liveness request to wake the service and then verifies heartbeat and scheduler recovery from PostgreSQL. It does not send recurring pings while the service is healthy, does not use the Render management/restart API, and does not wake for market-feed degradation alone. GitHub scheduled runs are best-effort, so recovery may be delayed by the ten-minute schedule or GitHub Actions availability; this is not a real-time uptime SLA or a guarantee of continuous compute.

For guaranteed continuous worker operation, Solo needs an always-on hosting plan or a suitable always-on compute environment. Current $0 hosting constraints do not provide that guarantee.

## Manual cold-start recovery

The manual workflow `.github/workflows/solo-staging-health-check.yml` remains a separate end-to-end acceptance action. It can be dispatched deliberately to exercise `/healthz` and then verify the separate API's database-backed readiness. The scheduled monitor uses the database classifier to decide whether the single worker liveness request is warranted.

## Safety

- The monitor uses read-only database credentials and requires TLS.
- It never calls exchange order endpoints or Render management/restart APIs; the only conditional HTTP action is a single unauthenticated worker liveness GET.
- It never changes ledger rows, risk limits, credentials, or Emergency Stop state.
- Any observed `orders_authorized=true` is reported as a safety violation.
- Stale market data is reported separately from a stopped worker process.
