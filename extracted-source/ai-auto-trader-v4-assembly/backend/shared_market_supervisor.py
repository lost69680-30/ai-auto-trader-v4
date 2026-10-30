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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    worker_env, api_env = build_child_environments(os.environ)
    max_delay = max(1.0, float(os.environ.get("PAPER_SUPERVISOR_MAX_RESTART_DELAY_SECONDS", "30")))
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
