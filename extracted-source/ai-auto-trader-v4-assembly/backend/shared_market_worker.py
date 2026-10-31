"""Standalone paper-only market-analysis worker publishing to shared PostgreSQL."""
from __future__ import annotations

import logging
import os
import signal
import threading
import time
from datetime import datetime, timedelta, timezone

from app.engines.paper_service_app import PaperServiceApplication
from backend.analysis_job_dispatcher import AnalysisJobDispatcher, market_feed_health_audit
from backend.durable_scheduler import DurableJobScheduler
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
        lease_seconds=max(90.0, float(os.environ.get("PAPER_MARKET_WORKER_LEASE_SECONDS", "90"))),
    )
    if not store.acquire_lease():
        raise SystemExit("another market worker owns the active PostgreSQL lease; refusing duplicate polling")

    app = None
    stop = threading.Event()
    scheduler = DurableJobScheduler(
        dsn, lease_seconds=max(30.0, float(os.environ.get("PAPER_SCHEDULED_JOB_LEASE_SECONDS", "60")))
    )
    schedule_interval = max(60, int(os.environ.get("PAPER_MARKET_HEALTH_AUDIT_INTERVAL_SECONDS", "300")))
    scheduler.register_interval(
        "market_feed_health_audit",
        interval_seconds=schedule_interval,
        first_run_at=datetime.now(timezone.utc) + timedelta(seconds=schedule_interval),
        payload={"scope": "market_feed"},
    )
    dispatcher = AnalysisJobDispatcher(
        scheduler,
        owner=os.environ.get("PAPER_ANALYSIS_DISPATCHER_ID", f"{worker_id[:110]}-analysis"),
        handlers={"market_feed_health_audit": market_feed_health_audit(store)},
    )

    def request_stop(signum, frame) -> None:
        logger.info("shutdown requested; stopping paper-only market worker")
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        # Acquire the durable lease before creating the paper runtime: create() performs
        # restart recovery and must never run in a duplicate worker process.
        app = PaperServiceApplication.from_env()
        while not stop.is_set():
            try:
                poll = app.worker.poll_once()
                store.publish(poll)
                report = app.process_poll(poll, now=time.time())
                cycle_status = str(report.analysis_status)
                analysis_error = cycle_status.partition(":")[2] if cycle_status.startswith("ANALYSIS_FAILED:") else None
                heartbeat_status = "DEGRADED" if analysis_error or cycle_status in {
                    "SKIPPED_UNHEALTHY_MARKET_DATA", "FUNDING_SYNC_DEGRADED",
                    "MARKET_UNIVERSE_STALE", "NO_HEALTHY_ALLOWLISTED_SYMBOLS",
                    "NO_ELIGIBLE_SYMBOLS", "ANALYZED_WITH_EXCLUSIONS",
                    "FUNDING_SYNC_ERROR", "MARKET_UNIVERSE_UNAVAILABLE",
                    "MARKET_UNIVERSE_REJECTED", "AUTHORIZATION_FLAG_REJECTED",
                    "ANALYSIS_RESULT_MISSING", "ANALYSIS_ELIGIBILITY_INVALID",
                } else "RUNNING"
                store.heartbeat(
                    heartbeat_status,
                    details={
                        "analysis_status": cycle_status,
                        "analysis_error": analysis_error,
                        "last_cycle_at": datetime.now(timezone.utc).isoformat(),
                        "healthy_symbols": list(report.healthy_symbols),
                        "unhealthy_symbols": list(report.unhealthy_symbols),
                        "orders_authorized": False,
                    },
                )
                logger.info(
                    "shared market cycle status=%s healthy=%s unhealthy=%s open_positions=%d orders_authorized=False",
                    report.analysis_status, report.healthy_symbols, report.unhealthy_symbols, report.open_positions,
                )
                try:
                    scheduler.enqueue_due()
                    dispatcher.run_once()
                except Exception:
                    logger.exception("durable read-only analysis scheduler cycle failed")
                delay = app.worker.next_delay_seconds
            except Exception as exc:
                logger.exception("shared market worker cycle failed; feed remains fail-closed")
                try:
                    store.heartbeat("DEGRADED", details={
                        "analysis_status": "ANALYSIS_FAILED:WorkerCycleError",
                        "analysis_error": type(exc).__name__,
                        "last_cycle_at": datetime.now(timezone.utc).isoformat(),
                        "orders_authorized": False,
                    })
                except Exception:
                    logger.exception("market worker heartbeat failed; worker lease may be lost")
                    stop.set()
                delay = 15.0
            stop.wait(delay)
    finally:
        try:
            store.release()
        finally:
            if app is not None:
                app.close()


if __name__ == "__main__":
    main()
