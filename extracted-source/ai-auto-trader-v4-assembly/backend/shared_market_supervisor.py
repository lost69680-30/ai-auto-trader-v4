"""Run Solo's API and paper-analysis worker as independently restarted processes."""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
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


def worker_heartbeat_requires_restart(
    status: Any, *, uptime_seconds: float, startup_grace_seconds: float = 180.0
) -> bool:
    """Return true only for a confirmed stale/missing worker after startup grace.

    A database read exception is handled by the caller and must not be treated as
    proof of worker failure. DEGRADED is deliberately not stale: a worker that is
    still publishing fresh health is allowed to recover its data source.
    """
    if uptime_seconds < startup_grace_seconds or not isinstance(status, dict):
        return False
    worker_status = status.get("worker_status")
    return (
        status.get("status") == "STALE"
        or worker_status in {"STOPPED", "ERROR"}
        or worker_status == "MISSING"
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    worker_env, api_env = build_child_environments(os.environ)
    max_delay = max(1.0, float(os.environ.get("PAPER_SUPERVISOR_MAX_RESTART_DELAY_SECONDS", "30")))
    max_market_age = max(5.0, float(os.environ.get("PAPER_MAX_MARKET_AGE_SECONDS", "90")))
    startup_grace = max(30.0, float(os.environ.get("PAPER_SUPERVISOR_WORKER_STARTUP_GRACE_SECONDS", "180")))
    health_check_interval = max(2.0, float(os.environ.get("PAPER_SUPERVISOR_WORKER_HEALTH_CHECK_SECONDS", "5")))
    database_url = os.environ.get("PAPER_DATABASE_URL", "").strip()
    market_store = None
    if database_url:
        from backend.shared_market_store import PostgresMarketFeedStore

        market_store = PostgresMarketFeedStore(
            database_url,
            worker_id=os.environ.get("PAPER_MARKET_WORKER_ID", "solo-market-worker"),
            max_age_seconds=max_market_age,
            lease_seconds=max(90.0, float(os.environ.get("PAPER_MARKET_WORKER_LEASE_SECONDS", "90"))),
        )
    if market_store is None:
        logger.warning("worker heartbeat monitor disabled because PAPER_DATABASE_URL is missing; worker will fail closed and process-exit supervision remains active")
    last_worker_health_check = 0.0
    modules = {
        "worker": ("backend.shared_market_worker", worker_env),
        "api": ("app.engines.read_only_api", api_env),
    }
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
        }

    try:
        start_child("worker")
        start_child("api")
        while not stopping:
            now = time.monotonic()
            for name, state in list(children.items()):
                proc = state["process"]
                exit_code = proc.poll()
                if exit_code is None:
                    if name == "worker" and market_store is not None and now - last_worker_health_check >= health_check_interval:
                        last_worker_health_check = now
                        try:
                            worker_status = market_store.status()
                        except Exception:
                            # A transient PostgreSQL read failure is not proof that the
                            # worker is dead. The API readiness check remains fail-closed.
                            logger.exception("worker heartbeat health check unavailable; preserving worker process")
                        else:
                            if worker_heartbeat_requires_restart(
                                worker_status,
                                uptime_seconds=now - state["started_at"],
                                startup_grace_seconds=startup_grace,
                            ):
                                logger.error(
                                    "worker heartbeat stale while process is alive; terminating for supervised recovery status=%s worker_status=%s age_seconds=%s",
                                    worker_status.get("status"),
                                    worker_status.get("worker_status"),
                                    worker_status.get("age_seconds"),
                                )
                                proc.terminate()
                    if now - state["started_at"] >= 30:
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
        for name, state in children.items():
            proc = state["process"]
            if proc.poll() is None:
                logger.info("stopping child process name=%s pid=%s", name, proc.pid)
                proc.terminate()
        deadline = time.monotonic() + 10.0
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
