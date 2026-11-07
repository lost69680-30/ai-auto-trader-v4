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

The scheduled workflow keeps database inspection SELECT-only. It sends no periodic liveness requests to a healthy or degraded-but-live worker. Only when the read-only classifier confirms `STOPPED` with `orders_authorized=false` does it send one unauthenticated `/healthz` wake request, then poll read-only state to verify worker and scheduler recovery. A degraded feed alone is never treated as a stopped process. Unknown or unsafe authorization state never triggers a request.

## GitHub setup

The repository needs the existing Actions secret:

- `SOLO_BACKUP_DATABASE_URL`: PostgreSQL connection string for the dedicated read-only `solo_backup_reader` role. The monitor requires TLS and sets the session read-only. Never expose this value in logs, source, or Android configuration.

The monitor workflow is `.github/workflows/solo-render-keepwarm.yml` (historical filename) and runs every ten minutes plus manual dispatch. It performs one read-only database classification and sends a single `/healthz` wake request only after a confirmed stopped-worker state. It never pings a healthy worker to keep a Free service warm and never uses Render management APIs. A second manual workflow, `.github/workflows/solo-independent-worker-watchdog.yml`, validates the monitor code and contracts.

## Render Free limitation

Render Free web services may stop after inactivity. The ten-minute GitHub schedule checks database state only. After a confirmed stale/stopped state, the monitor makes at most one `/healthz` wake request and verifies recovery from PostgreSQL. It does not send recurring keep-alive requests, call the Render management/restart API, or treat feed degradation as worker death. GitHub scheduled runs are best-effort and may be delayed, so this is not a real-time uptime SLA or a guarantee of continuous compute.

For guaranteed continuous worker operation, Solo needs an always-on hosting plan or a suitable always-on compute environment. Current $0 hosting constraints do not provide that guarantee.

## Manual cold-start recovery

The manual workflow `.github/workflows/solo-staging-health-check.yml` remains a separate end-to-end acceptance action. It can be dispatched deliberately to exercise `/healthz` and then verify the separate API's database-backed readiness. The scheduled monitor uses the database classifier to decide whether the single worker liveness request is warranted.

## Safety

- The monitor uses read-only database credentials and requires TLS.
- It never calls exchange order endpoints or Render management/restart APIs; the only HTTP action is at most one unauthenticated worker liveness GET per ten-minute monitor run.
- It never changes ledger rows, risk limits, credentials, or Emergency Stop state.
- Any observed `orders_authorized=true` is reported as a safety violation.
- Stale market data is reported separately from a stopped worker process.
