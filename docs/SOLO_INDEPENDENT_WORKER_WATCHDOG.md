# Solo independent worker watchdog

## What this adds

A GitHub-hosted scheduled monitor probes the Render worker service every ten minutes. It is outside the Render worker process and host. It validates the worker's minimal public `/healthz` response and, if the service is unreachable or the response is invalid, sends one restart request to the exact Render worker service. It then verifies recovery with bounded retries.

The monitor is deliberately limited to service-level liveness. The in-service supervisor remains responsible for child-process exits, PostgreSQL lease expiry, and scheduler-progress stalls. The public worker `/healthz` route is a liveness probe, not proof that market data or the scheduler is healthy.

## One-time GitHub setup

Add this repository Actions secret:

- `RENDER_API_KEY`: a Render API key with access to the Solo workspace. Create it in Render account settings and store it only in GitHub Actions secrets. Never commit it or put it in Android/app configuration.

The workflow is pinned to the current staging worker:
- Service: `solo-futures-websocket-staging`
- Service ID: `srv-dbhr5ign74is738detbg`
- Health URL: `https://solo-futures-websocket-staging.onrender.com/healthz`

If the secret is missing, the workflow can detect the outage but cannot restart the service; it fails with a clear error.

## Recovery and safety limits

- At most one Render restart request is issued per workflow run.
- After a restart request, the workflow polls for a valid liveness response for up to about five minutes.
- No database writes, market-data mutations, exchange credentials, order endpoints, or trading permissions are used.
- The worker must continue to report `mode=PAPER_ONLY` and `orders_authorized=false`; otherwise the probe is treated as invalid.
- No restart is triggered merely because market data is stale. The in-service watchdog owns worker-heartbeat and scheduler-progress recovery.
- GitHub Actions scheduled runs can be delayed or unavailable. This is independent of the Render worker process, but it is not a guaranteed real-time 24/7 monitoring SLA.
- Render Free may sleep or take time to wake. The probe has a bounded 90-second timeout; a successful liveness response after cold start avoids a restart.

## Acceptance still required

After merging and adding `RENDER_API_KEY`, run the workflow manually once. Confirm:
1. the workflow reads a valid `PAPER_ONLY` / `orders_authorized=false` health response;
2. no restart occurs during healthy operation;
3. recovery is verified after a controlled staging outage/restart test;
4. the worker's database lease, scheduler progress, and all market symbols are healthy after recovery.

Do not call the system fully independently monitored until these staging checks pass.
