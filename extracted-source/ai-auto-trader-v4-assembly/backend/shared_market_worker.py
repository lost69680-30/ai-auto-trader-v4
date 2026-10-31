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
from backend.historical_candle_archive import PostgresCandleArchive, archive_is_caught_up, archive_market_poll, backfill_older_symbol_history, backfill_symbol_history
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

    def request_stop(signum, frame) -> None:
        logger.info("shutdown requested; stopping paper-only market worker")
        stop.set()

    try:
        # All setup after lease acquisition is covered by finally so a startup failure
        # cannot leave the PostgreSQL worker lease held until its timeout.
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
        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
        # Acquire the durable lease before creating the paper runtime: create() performs
        # restart recovery and must never run in a duplicate worker process.
        app = PaperServiceApplication.from_env()
        testnet = str(os.environ.get("PAPER_MARKET_TESTNET", "true")).strip().lower() in {"1", "true", "yes"}
        if not testnet:
            raise SystemExit("shared historical archive is Testnet-only; refusing production market data")
        if app.config.candle_interval != "1m":
            raise SystemExit("shared historical archive requires PAPER_CANDLE_INTERVAL=1m")
        archive = PostgresCandleArchive(dsn)
        archive_lookback_days = int(os.environ.get("PAPER_MARKET_ARCHIVE_LOOKBACK_DAYS", "30"))
        if not 1 <= archive_lookback_days <= 365:
            raise SystemExit("PAPER_MARKET_ARCHIVE_LOOKBACK_DAYS must be between 1 and 365")
        while not stop.is_set():
            try:
                backfill_clock = datetime.now(timezone.utc)
                backfilled_count = 0
                for symbol in app.config.symbols_list:
                    backfilled_count += backfill_symbol_history(
                        archive, app.worker.source, symbol=symbol, market_type="FUTURES",
                        testnet=testnet, now=backfill_clock, page_size=1000, max_pages=3,
                    )
                archive_caught_up = archive_is_caught_up(
                    archive, symbols=app.config.symbols_list, market_type="FUTURES",
                    now=backfill_clock,
                )
                poll = app.worker.poll_once()
                store.publish(poll)
                archived_count = backfilled_count
                older_history_count = 0
                older_history_errors = []
                if archive_caught_up:
                    for symbol in app.config.symbols_list:
                        try:
                            older_history_count += backfill_older_symbol_history(
                                archive, app.worker.source, symbol=symbol, market_type="FUTURES",
                                testnet=testnet, now=backfill_clock,
                                lookback_days=archive_lookback_days, page_size=500, max_pages=1,
                            )
                        except Exception as history_exc:
                            older_history_errors.append(symbol)
                            logger.warning(
                                "older historical backfill failed symbol=%s error=%s",
                                symbol, type(history_exc).__name__,
                            )
                    archived_count += older_history_count
                    archived_count += archive_market_poll(
                        archive, poll, interval=app.config.candle_interval,
                        testnet=testnet, now=datetime.now(timezone.utc),
                    )
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
                        "archived_new_candles": archived_count,
                        "historical_archive_caught_up": archive_caught_up,
                        "historical_archive_lookback_days": archive_lookback_days,
                        "historical_archive_older_candles": older_history_count,
                        "historical_archive_backfill_errors": older_history_errors,
                        "orders_authorized": False,
                    },
                )
                logger.info(
                    "shared market cycle status=%s healthy=%s unhealthy=%s open_positions=%d archived_new_candles=%d archive_caught_up=%s orders_authorized=False",
                    report.analysis_status, report.healthy_symbols, report.unhealthy_symbols, report.open_positions, archived_count, archive_caught_up,
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
