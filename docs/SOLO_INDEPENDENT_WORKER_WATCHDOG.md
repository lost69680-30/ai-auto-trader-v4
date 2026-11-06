# Solo independent worker watchdog

## What this adds

A GitHub-hosted scheduled monitor runs every ten minutes, outside the Render API and worker services. It first calls the worker's minimal public `/healthz` endpoint to keep the Render Free web service awake, then calls the separate staging API's public `/readyz` endpoint, which reads the shared PostgreSQL worker heartbeat and validated market-feed readiness. The Python monitor is isolated in `scripts/solo_independent_watchdog.py` and has unit tests for safe restart classification.

The monitor always allows a 60-second cold-start/lease-renewal grace after a stale heartbeat is observed, even if the first liveness request times out, then distinguishes a confirmed worker-process/lease failure from ordinary data-feed degradation.
- `status=ready`, `worker_status=RUNNING`, and `orders_authorized=false`: healthy; no restart.
- `worker_status` is `STALE`, `STOPPED`, `ERROR`, or `MISSING`: confirmed worker heartbeat failure; one worker-service restart may be requested.
- API unreachable, invalid response, `UNKNOWN`, or worker still `RUNNING` but readiness is degraded: fail closed and do not restart the worker. This avoids restarting a healthy process for a market-data outage or an API/network problem.

After a restart request, the monitor makes one worker keepalive request and polls the independent readiness endpoint with bounded retries. It succeeds only when the worker lease and market readiness return to healthy.

The in-service supervisor remains responsible for worker child-process exits and scheduler-progress stalls. The external monitor is an independent service-level recovery layer; it does not replace the internal watchdog.

## One-time GitHub setup

Add this repository Actions secret:

- `RENDER_API_KEY`: a Render API key with permission to restart the Solo staging worker service. Store it only in GitHub Actions secrets. Never commit it or put it in Android/app configuration.

Pinned staging targets:
- Independent readiness API: `https://solo-ai-auto-trader-staging-api.onrender.com/readyz`
- API Render service: `solo-ai-auto-trader-staging-api`
- Worker Render service: `solo-futures-websocket-staging`
- Worker service ID: `srv-dbhr5ign74is738detbg`

The readiness API is on a separate Render service and reads the shared worker heartbeat. If the API cannot be reached, the workflow reports failure but deliberately does not restart the worker because worker failure cannot be confirmed.

## Recovery and safety limits

- Scheduled interval: every ten minutes; the worker liveness request also acts as a keepalive for Render Free, but GitHub Actions may delay scheduled runs. The recovery workflow timeout is 12 minutes.
- At most one Render restart request is issued per workflow run.
- Recovery is verified through the separate API's database-backed `/readyz` endpoint; the worker `/healthz` endpoint is used only for keepalive/liveness.
- No database writes, market-data mutations, exchange credentials, order endpoints, or trading permissions are used.
- The readiness payload must explicitly report `orders_authorized=false`.
- A stale market feed with a still-running worker does not trigger a restart.
- No Emergency Stop state is cleared or modified.
- This is independent of the Render worker process/host, but it is not a guaranteed real-time uptime SLA and depends on GitHub Actions and the separate API service being reachable.

## Acceptance still required

After merging and adding `RENDER_API_KEY`, run the workflow manually once. Confirm:
1. healthy `/readyz` returns without a restart;
2. a stale worker lease triggers exactly one restart request;
3. the workflow verifies worker recovery through `/readyz`;
4. a stale market feed with a running worker does not cause a restart;
5. the worker's lease, scheduler progress, and configured market symbols are healthy after recovery.

Do not call the system fully independently monitored until these staging checks pass.
