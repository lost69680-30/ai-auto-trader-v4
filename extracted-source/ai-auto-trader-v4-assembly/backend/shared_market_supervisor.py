"""Run Solo's API and paper-analysis worker as independently restarted processes."""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from collections.abc import Mapping
import threading
from typing import Any


logger = logging.getLogger(__name__)


def build_child_environments(environ: Mapping[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Return least-privilege worker and API environments without mutating parent env."""
    port = str(environ.get("PORT", "")).strip()
    if not port:
        raise ValueError("PORT is required by the API process")
    worker_env = dict(environ)
    worker_env.pop("PAPER_API_TOKEN", None)
    worker_env.pop("PAPER_API_HOST", None)
    worker_env.pop("PAPER_API_PORT", None)
    for secret_name in (
        "BINANCE_SPOT_TESTNET_API_KEY", "BINANCE_SPOT_TESTNET_API_SECRET",
        "BINANCE_API_KEY", "BINANCE_API_SECRET",
    ):
        worker_env.pop(secret_name, None)
    worker_env["PAPER_MARKET_ROLE"] = "worker"
    worker_env["PAPER_MARKET_SUPERVISOR_ENABLED"] = "false"

    api_env = dict(environ)
    for secret_name in (
        "BINANCE_SPOT_TESTNET_API_KEY", "BINANCE_SPOT_TESTNET_API_SECRET",
        "BINANCE_API_KEY", "BINANCE_API_SECRET",
    ):
        api_env.pop(secret_name, None)
    api_env["PAPER_MARKET_ROLE"] = "api"
    api_env["PAPER_MARKET_SUPERVISOR_ENABLED"] = "false"
    api_env["PAPER_RESEARCH_WORKER_ENABLED"] = "false"
    api_env["PAPER_API_HOST"] = "0.0.0.0"
    api_env["PAPER_API_PORT"] = port
    api_env["PAPER_API_HTTPS_TERMINATED"] = "1"
    return worker_env, api_env


def _start(module: str, env: dict[str, str]) -> subprocess.Popen:
    logger.info("starting child process module=%s", module)
    return subprocess.Popen([sys.executable, "-m", module], env=env)


def configured_child_shutdown_grace(environ: Mapping[str, str]) -> float:
    """Allow the worker's 15-second scheduler shutdown to finish before SIGKILL."""
    return max(15.0, float(environ.get("PAPER_SUPERVISOR_CHILD_SHUTDOWN_GRACE_SECONDS", "25")))



def configured_supervisor_modules(
    mode: str,
    worker_env: dict[str, str],
    api_env: dict[str, str],
) -> dict[str, tuple[str, dict[str, str]]]:
    """Select which supervised process roles this Render service owns."""
    normalized = str(mode).strip().lower()
    if normalized not in {"all", "worker", "api"}:
        raise ValueError("PAPER_MARKET_SUPERVISOR_MODE must be all, worker, or api")
    modules = {}
    if normalized in {"all", "worker"}:
        modules["worker"] = ("backend.shared_market_worker", worker_env)
    if normalized in {"all", "api"}:
        modules["api"] = ("app.engines.read_only_api", api_env)
    return modules



def worker_readiness_payload(
    snapshot: Any, *, max_age_seconds: float = 90.0,
    max_scheduler_stale_seconds: float = 900.0,
) -> tuple[dict[str, Any], int]:
    """Build a safe readiness response; unknown, stale, or unsafe state fails closed."""
    import math

    for value, name in (
        (max_age_seconds, "max_age_seconds"),
        (max_scheduler_stale_seconds, "max_scheduler_stale_seconds"),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{name} must be a finite positive number")

    state = snapshot if isinstance(snapshot, Mapping) else {}
    feed_status = state.get("status")
    worker_status = state.get("worker_status")
    age = state.get("age_seconds")
    lease_expired = state.get("lease_expired")
    process_alive = state.get("process_alive") is True
    orders_authorized = state.get("orders_authorized", False)
    if not isinstance(orders_authorized, bool):
        orders_authorized = None

    age_is_fresh = (
        isinstance(age, (int, float))
        and not isinstance(age, bool)
        and math.isfinite(age)
        and 0 <= age <= float(max_age_seconds)
    )
    details = state.get("details")
    scheduler = details.get("scheduled_jobs") if isinstance(details, Mapping) else None
    if not isinstance(scheduler, Mapping):
        scheduler = {}
    scheduler_status = scheduler.get("status")
    scheduler_progress_times = []
    for field in ("last_poll_started_at", "last_poll_at"):
        raw = scheduler.get(field)
        if not isinstance(raw, str):
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            continue
        scheduler_progress_times.append(parsed.astimezone(timezone.utc))
    scheduler_age = None
    if scheduler_progress_times:
        scheduler_age = (
            datetime.now(timezone.utc) - max(scheduler_progress_times)
        ).total_seconds()
    scheduler_is_fresh = (
        scheduler_status == "RUNNING"
        and isinstance(scheduler_age, (int, float))
        and math.isfinite(scheduler_age)
        and -1.0 <= scheduler_age <= float(max_scheduler_stale_seconds)
    )

    ready = (
        feed_status == "HEALTHY"
        and worker_status == "RUNNING"
        and age_is_fresh
        and lease_expired is False
        and process_alive
        and scheduler_is_fresh
        and orders_authorized is False
    )
    mode = "PAPER_ONLY" if orders_authorized is False else "SAFETY_VIOLATION"
    payload = {
        "status": "ready" if ready else "not_ready",
        "mode": mode,
        "feed_status": feed_status if isinstance(feed_status, str) else "UNKNOWN",
        "worker_status": worker_status if isinstance(worker_status, str) else "UNKNOWN",
        "scheduler_status": scheduler_status if isinstance(scheduler_status, str) else "UNKNOWN",
        "scheduler_age_seconds": max(0.0, scheduler_age) if scheduler_age is not None and math.isfinite(scheduler_age) else None,
        "process_alive": process_alive,
        "lease_expired": lease_expired if isinstance(lease_expired, bool) else None,
        "age_seconds": float(age) if age_is_fresh else None,
        "orders_authorized": orders_authorized,
    }
    return payload, 200 if ready else 503


def start_worker_health_server(
    port: int, *, host: str = "0.0.0.0",
    readiness_provider=None, max_age_seconds: float = 90.0,
    max_scheduler_stale_seconds: float = 900.0,
):
    """Serve separate liveness and data-aware readiness routes for the paper worker."""
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    class HealthHandler(BaseHTTPRequestHandler):
        def _respond(self, payload: dict[str, Any], status_code: int) -> None:
            body = json.dumps(payload, allow_nan=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.partition("?")[0]
            if path == "/healthz":
                self._respond({
                    "status": "ok",
                    "mode": "PAPER_ONLY",
                    "orders_authorized": False,
                }, 200)
                return
            if path == "/readyz":
                if not callable(readiness_provider):
                    snapshot = None
                else:
                    try:
                        snapshot = readiness_provider()
                    except Exception as exc:
                        logger.warning(
                            "worker readiness snapshot unavailable error_type=%s",
                            type(exc).__name__,
                        )
                        snapshot = None
                if snapshot is None:
                    snapshot = {
                        "status": "UNKNOWN",
                        "worker_status": "UNKNOWN",
                        "age_seconds": None,
                        "lease_expired": None,
                        "process_alive": False,
                        "orders_authorized": False,
                    }
                payload, status_code = worker_readiness_payload(
                    snapshot,
                    max_age_seconds=max_age_seconds,
                    max_scheduler_stale_seconds=max_scheduler_stale_seconds,
                )
                self._respond(payload, status_code)
                return
            self.send_error(404)

        def log_message(self, format, *args):
            logger.debug("worker health request: " + format, *args)

    server = ThreadingHTTPServer((host, int(port)), HealthHandler)
    thread = Thread(target=server.serve_forever, name="solo-worker-health", daemon=True)
    thread.start()
    logger.info("worker-only health endpoint listening host=%s port=%s", host, server.server_port)
    return server


def _scheduler_progress_is_stale(
    status: Mapping[str, Any], *, now: datetime, stale_seconds: float,
) -> bool:
    """Detect a stalled durable-job runner without mistaking a failed job for a dead thread."""
    details = status.get("details")
    if not isinstance(details, Mapping):
        return False
    scheduler = details.get("scheduled_jobs")
    if not isinstance(scheduler, Mapping):
        return False
    progress_times = []
    for field in ("last_poll_started_at", "last_poll_at"):
        raw = scheduler.get(field)
        if not isinstance(raw, str):
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            continue
        progress_times.append(parsed.astimezone(timezone.utc))
    if not progress_times:
        return False
    return (now.astimezone(timezone.utc) - max(progress_times)).total_seconds() > stale_seconds


def configured_scheduler_stale_seconds(environ: Mapping[str, str]) -> int:
    """Read the scheduler watchdog threshold, bounded to avoid aggressive restarts."""
    raw = environ.get("PAPER_SUPERVISOR_SCHEDULER_STALE_SECONDS", "900")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("PAPER_SUPERVISOR_SCHEDULER_STALE_SECONDS must be between 60 and 3600") from exc
    if isinstance(value, bool) or not 60 <= value <= 3600:
        raise ValueError("PAPER_SUPERVISOR_SCHEDULER_STALE_SECONDS must be between 60 and 3600")
    return value


def worker_heartbeat_requires_restart(
    status: Any, *, uptime_seconds: float, startup_grace_seconds: float = 180.0,
    scheduler_stale_seconds: float = 900.0, now: datetime | None = None,
) -> bool:
    """Restart only stale worker leases or workers whose durable scheduler stopped progressing."""
    if uptime_seconds < startup_grace_seconds or not isinstance(status, dict):
        return False
    worker_status = status.get("worker_status")
    if (
        worker_status in {"STALE", "STOPPED", "ERROR", "MISSING"}
        or status.get("lease_expired") is True
    ):
        return True
    if (
        isinstance(scheduler_stale_seconds, bool)
        or not isinstance(scheduler_stale_seconds, (int, float))
        or scheduler_stale_seconds <= 0
    ):
        raise ValueError("scheduler_stale_seconds must be positive")
    return _scheduler_progress_is_stale(
        status, now=now or datetime.now(timezone.utc),
        stale_seconds=float(scheduler_stale_seconds),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    worker_env, api_env = build_child_environments(os.environ)
    max_delay = max(1.0, float(os.environ.get("PAPER_SUPERVISOR_MAX_RESTART_DELAY_SECONDS", "30")))
    max_market_age = max(5.0, float(os.environ.get("PAPER_MAX_MARKET_AGE_SECONDS", "90")))
    startup_grace = max(30.0, float(os.environ.get("PAPER_SUPERVISOR_WORKER_STARTUP_GRACE_SECONDS", "180")))
    health_check_interval = max(2.0, float(os.environ.get("PAPER_SUPERVISOR_WORKER_HEALTH_CHECK_SECONDS", "5")))
    scheduler_stale_seconds = configured_scheduler_stale_seconds(os.environ)
    restart_stable_seconds = max(60.0, float(os.environ.get("PAPER_SUPERVISOR_RESTART_STABLE_SECONDS", "300")))
    database_url = os.environ.get("PAPER_DATABASE_URL", "").strip()
    mode = os.environ.get("PAPER_MARKET_SUPERVISOR_MODE", "all")
    modules = configured_supervisor_modules(mode, worker_env, api_env)
    worker_readiness_state: dict[str, Any] = {
        "status": "STARTING",
        "worker_status": "STARTING",
        "age_seconds": None,
        "lease_expired": None,
        "process_alive": False,
        "orders_authorized": False,
    }
    worker_readiness_lock = threading.Lock()

    def readiness_provider() -> dict[str, Any]:
        with worker_readiness_lock:
            return dict(worker_readiness_state)

    def publish_readiness(**values: Any) -> None:
        with worker_readiness_lock:
            worker_readiness_state.update(values)

    worker_health_server = None
    if str(mode).strip().lower() == "worker":
        worker_health_server = start_worker_health_server(
            int(os.environ["PORT"]),
            readiness_provider=readiness_provider,
            max_age_seconds=max_market_age,
        )
    market_store = None
    if "worker" in modules and database_url:
        from backend.shared_market_store import PostgresMarketFeedStore

        market_store = PostgresMarketFeedStore(
            database_url,
            worker_id=os.environ.get("PAPER_MARKET_WORKER_ID", "solo-market-worker"),
            max_age_seconds=max_market_age,
            lease_seconds=max(90.0, float(os.environ.get("PAPER_MARKET_WORKER_LEASE_SECONDS", "90"))),
        )
    if "worker" in modules and market_store is None:
        logger.warning("worker heartbeat monitor disabled because PAPER_DATABASE_URL is missing; worker will fail closed and process-exit supervision remains active")
    last_worker_health_check = 0.0
    children: dict[str, dict[str, Any]] = {}
    stopping = False

    def request_stop(signum, frame) -> None:
        nonlocal stopping
        logger.info("supervisor received shutdown signal=%s", signum)
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    def start_child(name: str, *, restart_delay: float = 1.0) -> None:
        module, env = modules[name]
        proc = _start(module, env)
        children[name] = {
            "process": proc, "started_at": time.monotonic(),
            "restart_delay": restart_delay, "next_start_at": None,
            "healthy_since": None,
        }
        if name == "worker":
            publish_readiness(
                status="STARTING", worker_status="STARTING",
                age_seconds=None, lease_expired=None,
                process_alive=True, orders_authorized=False,
            )

    try:
        for child_name in modules:
            start_child(child_name)
        while not stopping:
            now = time.monotonic()
            for name, state in list(children.items()):
                proc = state["process"]
                exit_code = proc.poll()
                if name == "worker" and exit_code is not None:
                    publish_readiness(
                        status="STOPPED", worker_status="STOPPED",
                        age_seconds=None, lease_expired=True,
                        process_alive=False, orders_authorized=False,
                    )
                elif name == "worker":
                    publish_readiness(process_alive=True)
                if exit_code is None:
                    if name == "worker" and market_store is None:
                        publish_readiness(
                            status="UNKNOWN", worker_status="UNKNOWN",
                            age_seconds=None, lease_expired=None,
                            process_alive=True, orders_authorized=False,
                        )
                    if name == "worker" and market_store is not None and now - last_worker_health_check >= health_check_interval:
                        last_worker_health_check = now
                        try:
                            worker_status = market_store.status()
                        except Exception as exc:
                            # A transient PostgreSQL read failure is not proof that the
                            # worker is dead, but external readiness must fail closed.
                            state["healthy_since"] = None
                            publish_readiness(
                                status="UNKNOWN", worker_status="UNKNOWN",
                                age_seconds=None, lease_expired=None,
                                process_alive=True, orders_authorized=False,
                            )
                            logger.warning(
                                "worker heartbeat health check unavailable error_type=%s; preserving worker process",
                                type(exc).__name__,
                            )
                        else:
                            publish_readiness(
                                status=worker_status.get("status"),
                                worker_status=worker_status.get("worker_status"),
                                age_seconds=worker_status.get("age_seconds"),
                                lease_expired=worker_status.get("lease_expired"),
                                process_alive=True,
                                orders_authorized=worker_status.get("orders_authorized", False),
                            )
                            restart_required = worker_heartbeat_requires_restart(
                                worker_status,
                                uptime_seconds=now - state["started_at"],
                                startup_grace_seconds=startup_grace,
                                scheduler_stale_seconds=scheduler_stale_seconds,
                            )
                            if restart_required:
                                scheduler = (worker_status.get("details") or {}).get("scheduled_jobs", {})
                                logger.error(
                                    "worker reliability watchdog detected stalled health; terminating for supervised recovery status=%s worker_status=%s age_seconds=%s scheduler_last_poll_at=%s",
                                    worker_status.get("status"),
                                    worker_status.get("worker_status"),
                                    worker_status.get("age_seconds"),
                                    scheduler.get("last_poll_at") if isinstance(scheduler, dict) else None,
                                )
                                state["healthy_since"] = None
                                proc.terminate()
                            else:
                                age = worker_status.get("age_seconds")
                                healthy = (
                                    worker_status.get("status") in {"HEALTHY", "DEGRADED"}
                                    and worker_status.get("worker_status") in {"RUNNING", "DEGRADED"}
                                    and isinstance(age, (int, float))
                                    and 0 <= age <= max_market_age
                                )
                                if healthy:
                                    if state["healthy_since"] is None:
                                        state["healthy_since"] = now
                                    elif now - state["healthy_since"] >= restart_stable_seconds:
                                        state["restart_delay"] = 1.0
                                else:
                                    state["healthy_since"] = None
                    elif name == "api" and now - state["started_at"] >= restart_stable_seconds:
                        state["restart_delay"] = 1.0
                    continue
                if state["next_start_at"] is None:
                    delay = min(state["restart_delay"], max_delay)
                    logger.error(
                        "child process exited module=%s code=%s; restarting in %.1fs",
                        modules[name][0], exit_code, delay,
                    )
                    state["next_start_at"] = now + delay
                    state["restart_delay"] = min(delay * 2, max_delay)
                elif now >= state["next_start_at"]:
                    start_child(name, restart_delay=state["restart_delay"])
            time.sleep(1.0)
    finally:
        if worker_health_server is not None:
            logger.info("stopping worker-only health endpoint")
            worker_health_server.shutdown()
            worker_health_server.server_close()
        for name, state in children.items():
            proc = state["process"]
            if proc.poll() is None:
                logger.info("stopping child process name=%s pid=%s", name, proc.pid)
                proc.terminate()
        deadline = time.monotonic() + configured_child_shutdown_grace(os.environ)
        for state in children.values():
            proc = state["process"]
            if proc.poll() is None:
                try:
                    proc.wait(timeout=max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        logger.info("supervisor shutdown complete")


if __name__ == "__main__":
    main()
