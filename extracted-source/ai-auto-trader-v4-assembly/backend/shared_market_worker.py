"""Standalone paper-only market-analysis worker publishing to shared PostgreSQL."""
from __future__ import annotations

import logging
import os
import signal
import threading
import time

from app.engines.paper_service_app import PaperServiceApplication
from backend.shared_market_store import PostgresMarketFeedStore


logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    dsn = os.environ.get("PAPER_DATABASE_URL", "").strip()
    if not dsn:
        raise SystemExit("PAPER_DATABASE_URL is required; refusing local-only market feed storage.")
    max_age = float(os.environ.get("PAPER_MAX_MARKET_AGE_SECONDS", "90"))
    worker_id = os.environ.get("PAPER_MARKET_WORKER_ID", "solo-market-worker")
    store = PostgresMarketFeedStore(
        dsn, worker_id=worker_id, max_age_seconds=max_age,
        lease_seconds=max(30.0, float(os.environ.get("PAPER_MARKET_WORKER_LEASE_SECONDS", "30"))),
    )
    app = PaperServiceApplication.from_env()
    stop = threading.Event()

    def request_stop(signum, frame) -> None:
        logger.info("shutdown requested; stopping paper-only market worker")
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    if not store.acquire_lease():
        app.close()
        raise SystemExit("another market worker owns the active PostgreSQL lease; refusing duplicate polling")

    try:
        while not stop.is_set():
            try:
                poll = app.worker.poll_once()
                store.publish(poll)
                report = app.process_poll(poll, now=time.time())
                logger.info(
                    "shared market cycle status=%s healthy=%s unhealthy=%s open_positions=%d orders_authorized=False",
                    report.analysis_status, report.healthy_symbols, report.unhealthy_symbols, report.open_positions,
                )
                delay = app.worker.next_delay_seconds
            except Exception as exc:
                logger.exception("shared market worker cycle failed; feed remains fail-closed")
                try:
                    store.heartbeat("DEGRADED", details={"error": type(exc).__name__, "orders_authorized": False})
                except Exception:
                    logger.exception("market worker heartbeat failed; worker lease may be lost")
                    stop.set()
                delay = 15.0
            stop.wait(delay)
    finally:
        try:
            store.release()
        finally:
            app.close()


if __name__ == "__main__":
    main()
