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

The scheduled workflow does not call the worker's `/healthz` endpoint, does not wake Render, does not restart the service, and does not write to Supabase.

## GitHub setup

The repository needs the existing Actions secret:

- `SOLO_BACKUP_DATABASE_URL`: PostgreSQL connection string for the dedicated read-only `solo_backup_reader` role. The monitor requires TLS and sets the session read-only. Never expose this value in logs, source, or Android configuration.

The monitor workflow is `.github/workflows/solo-render-keepwarm.yml` (historical filename; now a read-only monitor) and runs every ten minutes plus manual dispatch. A second manual workflow, `.github/workflows/solo-independent-worker-watchdog.yml`, runs the same read-only check on demand.

## Render Free limitation

Render Free web services may stop after inactivity. A stopped heartbeat is therefore reported as stopped/stale; the monitor deliberately does not issue repeated pings or automatic restarts to bypass the free-tier idle-sleep behaviour. GitHub scheduled runs are also best-effort and are not a real-time uptime SLA.

For guaranteed continuous worker operation, Solo needs an always-on hosting plan or a suitable always-on compute environment. Current $0 hosting constraints do not provide that guarantee.

## Manual cold-start recovery

The manual workflow `.github/workflows/solo-staging-health-check.yml` is intentionally separate. When deliberately dispatched, it requests the worker's `/healthz` route once to wake the service, then checks the separate API's database-backed `/readyz` response. It is a manual recovery/acceptance action, not a scheduled keep-alive.

## Safety

- The monitor uses read-only database credentials and requires TLS.
- It never calls exchange order endpoints or Render restart APIs.
- It never changes ledger rows, risk limits, credentials, or Emergency Stop state.
- Any observed `orders_authorized=true` is reported as a safety violation.
- Stale market data is reported separately from a stopped worker process.
