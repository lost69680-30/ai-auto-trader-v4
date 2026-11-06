from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

CONFIRMED_WORKER_FAILURES = {"STALE", "STOPPED", "ERROR", "MISSING"}


def classify_readiness(payload: Any, error: str | None = None) -> tuple[str, str | None, str]:
    """Classify the independent API response without confusing feed degradation with worker death."""
    if error:
        return "indeterminate", None, error
    if not isinstance(payload, dict) or payload.get("orders_authorized") is not False:
        return "indeterminate", None, "readiness response failed paper-only safety validation"
    worker_status = payload.get("worker_status")
    if payload.get("status") == "ready" and worker_status == "RUNNING":
        return "healthy", worker_status, "worker lease and market readiness are healthy"
    if worker_status in CONFIRMED_WORKER_FAILURES:
        return "worker_failed", worker_status, "API reports a stale or stopped worker heartbeat"
    return "indeterminate", worker_status, "API is reachable but worker failure is not confirmed"


def valid_worker_liveness(payload: Any, error: str | None = None) -> bool:
    """Accept only the worker's minimal paper-only liveness response."""
    return (
        error is None
        and isinstance(payload, dict)
        and payload.get("status") == "ok"
        and payload.get("mode") == "PAPER_ONLY"
        and payload.get("orders_authorized") is False
    )


def get_json(url: str, timeout: int) -> tuple[Any, str | None]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "Cache-Control": "no-cache"},
        method="GET",
    )
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    except Exception as exc:
        return None, type(exc).__name__
    try:
        return json.loads(response.read(4096).decode("utf-8")), None
    except Exception:
        return None, "invalid JSON response"
    finally:
        response.close()


def main() -> int:
    ready_url = os.environ["API_READY_URL"]
    worker_url = os.environ["WORKER_LIVENESS_URL"]
    service_id = os.environ["RENDER_WORKER_SERVICE_ID"]
    api_key = os.environ.get("RENDER_API_KEY", "").strip()

    # This request keeps a Render Free worker web service awake. Its response is
    # liveness only; the independent API/database heartbeat decides readiness.
    worker_payload, worker_error = get_json(worker_url, timeout=60)
    worker_alive = valid_worker_liveness(worker_payload, worker_error)
    if worker_alive:
        print("Worker liveness responded with PAPER_ONLY and orders_authorized=false.")
    else:
        print(f"Worker liveness probe did not validate: {worker_error or 'invalid response'}")

    ready_payload, ready_error = get_json(ready_url, timeout=60)
    state, worker_status, reason = classify_readiness(ready_payload, ready_error)
    if state == "healthy":
        print("Independent readiness probe confirms healthy paper-only worker; no restart needed.")
        return 0

    print(f"Readiness probe: state={state}, worker_status={worker_status}, reason={reason}")
    if state != "worker_failed":
        print("::error::Worker failure is not confirmed; refusing an unsafe or unnecessary restart.")
        return 1

    # Always allow a startup/wake grace period, even if the first liveness request
    # timed out. A cold-start timeout is not proof that the worker is dead.
    print("Worker heartbeat is stale; allowing 60 seconds for a cold start or lease renewal.")
    time.sleep(60)
    ready_payload, ready_error = get_json(ready_url, timeout=60)
    state, worker_status, reason = classify_readiness(ready_payload, ready_error)
    if state == "healthy":
        print("Worker heartbeat recovered during startup grace; no restart needed.")
        return 0
    if state != "worker_failed":
        print(f"::error::Worker failure is no longer confirmed; refusing restart: {reason}")
        return 1

    if not api_key:
        print("::error::RENDER_API_KEY is not configured; worker failure detected but restart is unavailable.")
        return 1

    # One restart request per run, only after two independent stale-heartbeat reads.
    restart_url = f"https://api.render.com/v1/services/{service_id}/restart"
    request = urllib.request.Request(
        restart_url,
        data=b"",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status not in (200, 202):
                raise RuntimeError(f"Render restart returned HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        print(f"::error::Render restart request failed with HTTP {exc.code}")
        return 1
    except Exception as exc:
        print(f"::error::Render restart request failed: {type(exc).__name__}")
        return 1

    print("Render accepted one worker-service restart request; verifying database-backed recovery.")
    # A single keepalive request wakes the worker service; subsequent polling is
    # through the separate API and never triggers another restart in this run.
    get_json(worker_url, timeout=60)
    for attempt in range(1, 7):
        time.sleep(20)
        ready_payload, ready_error = get_json(ready_url, timeout=30)
        state, worker_status, reason = classify_readiness(ready_payload, ready_error)
        if state == "healthy":
            print(f"Worker lease and market readiness recovered (check {attempt}/6).")
            return 0
        print(f"Recovery check {attempt}/6: state={state}, worker_status={worker_status}, reason={reason}")

    print("::error::Worker did not return to healthy readiness within the verification window.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
