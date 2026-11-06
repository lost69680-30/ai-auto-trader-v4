"""Standalone paper-only market-analysis worker publishing to shared PostgreSQL."""
from __future__ import annotations

import logging
import math
import os
import signal
import threading
import time
from datetime import datetime, timedelta, timezone

from app.engines.paper_service_app import PaperServiceApplication
from backend.analysis_job_dispatcher import AnalysisJobDispatcher, market_feed_health_audit
from backend.durable_scheduler import DurableJobScheduler
from backend.market_intelligence_scan import market_intelligence_scan
from backend.historical_candle_archive import PostgresCandleArchive, archive_is_caught_up, archive_market_poll, backfill_older_symbol_history, backfill_symbol_history
from backend.shared_market_store import PostgresMarketFeedStore


logger = logging.getLogger(__name__)


def _env_bool(name: str, *, default: bool = False) -> bool:
    """Parse an opt-in feature flag; missing values stay disabled by default."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def create_candle_archive(dsn: str, *, enabled: bool):
    """Create the persistent raw-candle writer only after explicit opt-in."""
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be an explicit boolean")
    return PostgresCandleArchive(dsn) if enabled else None


def run_scheduled_jobs(
    stop: threading.Event, scheduler: DurableJobScheduler,
    dispatcher: AnalysisJobDispatcher, *, poll_interval: float = 1.0,
    health: dict | None = None, health_lock=None,
) -> None:
    """Run durable analysis jobs off the market-feed thread and publish progress."""
    if not isinstance(stop, threading.Event):
        raise ValueError("stop must be a threading.Event")
    if not isinstance(poll_interval, (int, float)) or poll_interval <= 0:
        raise ValueError("poll_interval must be positive")
    if health is not None and not isinstance(health, dict):
        raise ValueError("health must be a dictionary when supplied")

    def publish_health(**values):
        if health is None:
            return
        if health_lock is None:
            health.update(values)
        else:
            with health_lock:
                health.update(values)

    while not stop.is_set():
        publish_health(last_poll_started_at=datetime.now(timezone.utc).isoformat())
        scheduler_error = None
        try:
            enqueued = scheduler.enqueue_due()
            dispatcher.run_once()
        except Exception as exc:
            scheduler_error = type(exc).__name__
            logger.exception("durable read-only analysis scheduler thread failed")
            enqueued = 0
        dispatch_status = getattr(dispatcher, "last_run_status", "IDLE")
        dispatch_error = getattr(dispatcher, "last_error", None)
        degraded = scheduler_error is not None or dispatch_error is not None or dispatch_status == "LEASE_LOST"
        publish_health(
            status="DEGRADED" if degraded else "RUNNING",
            last_poll_at=datetime.now(timezone.utc).isoformat(),
            last_error=scheduler_error or dispatch_error,
            last_dispatch_status=dispatch_status,
            jobs_enqueued=enqueued,
        )
        stop.wait(float(poll_interval))


def run_equity_snapshot_scheduler(
    stop: threading.Event,
    ledger,
    *,
    initial_equity: float,
    interval_seconds: float = 300.0,
    max_mark_age_seconds: float = 30.0,
    clock=time.time,
) -> None:
    """Persist truthful paper-equity snapshots under the worker's single-owner lease.

    The baseline is supplied by the server-side PaperServiceConfig, never by an
    Android/API request. Snapshot persistence and its audit event are atomic in
    the ledger. Stale marks are persisted as unhealthy and are never presented as
    fresh; ledger errors are logged and retried on the next interval.
    """
    if not isinstance(stop, threading.Event):
        raise ValueError("stop must be a threading.Event")
    for name, value in (("initial_equity", initial_equity),
                        ("interval_seconds", interval_seconds),
                        ("max_mark_age_seconds", max_mark_age_seconds)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
    if initial_equity <= 0:
        raise ValueError("initial_equity must be positive")
    if interval_seconds < 1 or max_mark_age_seconds <= 0 or max_mark_age_seconds > 30:
        raise ValueError("snapshot interval or mark age limit is outside the safe range")
    if not callable(clock):
        raise ValueError("clock must be callable")

    while not stop.is_set():
        try:
            report = ledger.mark_to_market_risk_report(
                initial_equity=float(initial_equity),
                as_of=float(clock()),
                max_mark_age_seconds=float(max_mark_age_seconds),
            )
            logger.info(
                "paper equity snapshot persisted equity=%s marks_fresh=%s open_positions=%s orders_authorized=False",
                report.account_equity, report.marks_fresh, report.open_position_count,
            )
            if not report.marks_fresh:
                logger.warning("paper equity snapshot has stale or invalid position marks; risk display must remain degraded")
        except Exception:
            logger.exception("paper equity snapshot failed; risk display remains unavailable until a valid snapshot is persisted")
        stop.wait(float(interval_seconds))


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
    job_runner = None
    snapshot_runner = None
    stop = threading.Event()
    process_started_at = datetime.now(timezone.utc).isoformat()

    def request_stop(signum, frame) -> None:
        logger.info("shutdown requested; stopping paper-only market worker")
        stop.set()

    try:
        # All setup after lease acquisition is covered by finally so a startup failure
        # cannot leave the PostgreSQL worker lease held until its timeout.
        scheduler = DurableJobScheduler(
            dsn, lease_seconds=max(300.0, float(os.environ.get("PAPER_SCHEDULED_JOB_LEASE_SECONDS", "300")))
        )
        schedule_interval = max(60, int(os.environ.get("PAPER_MARKET_HEALTH_AUDIT_INTERVAL_SECONDS", "300")))
        scheduler.register_interval(
            "market_feed_health_audit",
            interval_seconds=schedule_interval,
            first_run_at=datetime.now(timezone.utc) + timedelta(seconds=schedule_interval),
            payload={"scope": "market_feed"},
        )
        intelligence_enabled = str(
            os.environ.get("PAPER_MARKET_INTELLIGENCE_ENABLED", "true")
        ).strip().lower() in {"1", "true", "yes", "on"}
        if intelligence_enabled:
            intelligence_interval = max(
                300, int(os.environ.get("PAPER_MARKET_INTELLIGENCE_INTERVAL_SECONDS", "900"))
            )
            scheduler.register_interval(
                "market_intelligence_scan",
                interval_seconds=intelligence_interval,
                first_run_at=datetime.now(timezone.utc) + timedelta(seconds=60),
                payload={"scope": "market_intelligence"},
            )
        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
        # Acquire the durable lease before creating the paper runtime: create() performs
        # restart recovery and must never run in a duplicate worker process.
        app = PaperServiceApplication.from_env()
        snapshot_interval = max(60, int(os.environ.get("PAPER_EQUITY_SNAPSHOT_INTERVAL_SECONDS", "300")))
        snapshot_max_mark_age = min(30.0, max(1.0, float(os.environ.get("PAPER_EQUITY_MAX_MARK_AGE_SECONDS", "30"))))
        testnet = str(os.environ.get("PAPER_MARKET_TESTNET", "true")).strip().lower() in {"1", "true", "yes"}
        archive_enabled = _env_bool("PAPER_CANDLE_ARCHIVE_ENABLED", default=False)
        if archive_enabled:
            if not testnet:
                raise SystemExit("historical candle archive is Testnet-only; refusing production market data")
            if app.config.candle_interval != "1m":
                raise SystemExit("historical candle archive requires PAPER_CANDLE_INTERVAL=1m")
        archive = create_candle_archive(dsn, enabled=archive_enabled)
        if archive is None:
            logger.info("stateless candle mode enabled; raw candles are fetched on demand and are not persisted")
        else:
            logger.warning("permanent candle archive explicitly enabled; PAPER_CANDLE_ARCHIVE_ENABLED=true")
        snapshot_runner = threading.Thread(
            target=run_equity_snapshot_scheduler,
            args=(stop, app.ledger),
            kwargs={
                "initial_equity": app.config.simulated_equity,
                "interval_seconds": snapshot_interval,
                "max_mark_age_seconds": snapshot_max_mark_age,
            },
            name=f"{worker_id[:80]}-equity-snapshots",
            daemon=True,
        )
        snapshot_runner.start()
        intelligence_handlers = {"market_feed_health_audit": market_feed_health_audit(store)}
        if intelligence_enabled:
            intelligence_candle_limit = int(
                os.environ.get("PAPER_MARKET_INTELLIGENCE_CANDLE_LIMIT", "200")
            )
            intelligence_fetch_workers = int(
                os.environ.get("PAPER_MARKET_INTELLIGENCE_FETCH_WORKERS", "12")
            )
            intelligence_handlers["market_intelligence_scan"] = market_intelligence_scan(
                store, app.worker.source, app.config.symbols,
                market_type="FUTURES", candle_limit=intelligence_candle_limit,
                max_fetch_workers=intelligence_fetch_workers,
            )
        dispatcher = AnalysisJobDispatcher(
            scheduler,
            owner=os.environ.get("PAPER_ANALYSIS_DISPATCHER_ID", f"{worker_id[:110]}-analysis"),
            handlers=intelligence_handlers,
        )
        archive_lookback_days = 30
        if archive_enabled:
            archive_lookback_days = int(os.environ.get("PAPER_MARKET_ARCHIVE_LOOKBACK_DAYS", "30"))
            if not 1 <= archive_lookback_days <= 365:
                raise SystemExit("PAPER_MARKET_ARCHIVE_LOOKBACK_DAYS must be between 1 and 365")

        scheduler_poll_interval = max(
            1.0, min(30.0, float(os.environ.get("PAPER_SCHEDULED_JOB_POLL_INTERVAL_SECONDS", "5")))
        )
        scheduler_health = {
            "status": "STARTING", "last_poll_started_at": None, "last_poll_at": None,
            "last_error": None, "last_dispatch_status": "IDLE", "jobs_enqueued": 0,
        }
        scheduler_health_lock = threading.Lock()
        job_runner = threading.Thread(
            target=run_scheduled_jobs,
            args=(stop, scheduler, dispatcher),
            kwargs={
                "poll_interval": scheduler_poll_interval,
                "health": scheduler_health,
                "health_lock": scheduler_health_lock,
            },
            name=f"{worker_id[:80]}-scheduled-jobs",
            daemon=True,
        )
        job_runner.start()

        while not stop.is_set():
            cycle_started_at = datetime.now(timezone.utc).isoformat()
            cycle_started_monotonic = time.monotonic()
            try:
                backfill_clock = datetime.now(timezone.utc)
                archived_count = 0
                archive_caught_up = None
                older_history_count = 0
                older_history_errors = []
                if archive is not None:
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
                    archived_count = backfilled_count
                poll = app.worker.poll_once()
                store.publish(poll)
                if archive is not None and archive_caught_up:
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
                with scheduler_health_lock:
                    scheduler_health_snapshot = dict(scheduler_health)
                heartbeat_status = "DEGRADED" if (
                    analysis_error or scheduler_health_snapshot.get("status") == "DEGRADED"
                ) or cycle_status in {
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
                        "process_id": os.getpid(),
                        "process_started_at": process_started_at,
                        "last_cycle_started_at": cycle_started_at,
                        "last_cycle_at": datetime.now(timezone.utc).isoformat(),
                        "cycle_duration_seconds": round(time.monotonic() - cycle_started_monotonic, 3),
                        "scheduled_jobs": scheduler_health_snapshot,
                        "healthy_symbols": list(report.healthy_symbols),
                        "unhealthy_symbols": list(report.unhealthy_symbols),
                        "archived_new_candles": archived_count,
                        "historical_archive_caught_up": archive_caught_up,
                        "candle_archive_enabled": archive is not None,
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
                if job_runner is not None and not job_runner.is_alive():
                    raise RuntimeError("durable scheduled-job runner stopped unexpectedly")
                delay = app.worker.next_delay_seconds
            except Exception as exc:
                logger.exception("shared market worker cycle failed; feed remains fail-closed")
                try:
                    store.heartbeat("DEGRADED", details={
                        "analysis_status": "ANALYSIS_FAILED:WorkerCycleError",
                        "analysis_error": type(exc).__name__,
                        "process_id": os.getpid(),
                        "process_started_at": process_started_at,
                        "last_cycle_started_at": cycle_started_at,
                        "last_cycle_at": datetime.now(timezone.utc).isoformat(),
                        "cycle_duration_seconds": round(time.monotonic() - cycle_started_monotonic, 3),
                        "scheduled_jobs": dict(scheduler_health),
                        "orders_authorized": False,
                    })
                except Exception:
                    logger.exception("market worker heartbeat failed; worker lease may be lost")
                    stop.set()
                delay = 15.0
            stop.wait(delay)
    finally:
        stop.set()
        if job_runner is not None:
            shutdown_grace = max(5.0, float(os.environ.get("PAPER_SCHEDULED_JOB_SHUTDOWN_GRACE_SECONDS", "15")))
            job_runner.join(timeout=shutdown_grace)
            if job_runner.is_alive():
                logger.warning("scheduled-job runner did not stop within shutdown grace; its durable job lease will expire for safe retry")
        if snapshot_runner is not None:
            snapshot_runner.join(timeout=5.0)
            if snapshot_runner.is_alive():
                logger.warning("equity snapshot runner did not stop within shutdown grace")
        try:
            store.release()
        finally:
            if app is not None:
                app.close()


if __name__ == "__main__":
    main()
