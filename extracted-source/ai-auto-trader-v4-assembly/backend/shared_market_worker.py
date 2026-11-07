"""Standalone paper-only market-analysis worker publishing to shared PostgreSQL."""
from __future__ import annotations

import logging
import math
import os
import re
import signal
import threading
import time
from datetime import datetime, timedelta, timezone

from app.engines.paper_service_app import PaperServiceApplication
from backend.analysis_job_dispatcher import AnalysisJobDispatcher, market_feed_health_audit
from backend.durable_scheduler import DurableJobScheduler
from backend.market_data_adapter import BinanceSpotMarketDataAdapter
from backend.opportunity_coordinator import OpportunityCoordinator
from backend.opportunity_store import PostgresOpportunityStore
from backend.signal_watcher import MarketTick
from backend.historical_candle_archive import PostgresCandleArchive, archive_is_caught_up, archive_market_poll, backfill_older_symbol_history, backfill_symbol_history
from backend.shared_market_store import PostgresMarketFeedStore


logger = logging.getLogger(__name__)


def disable_optional_intelligence_schedules(scheduler: DurableJobScheduler) -> None:
    """Disable persisted opportunity schedules when research is not opted in."""
    first_run = datetime.now(timezone.utc) + timedelta(days=3650)
    disabled = (
        ("market_intelligence_scan", {"scope": "continuous_opportunity_search"}),
        ("market_intelligence_scan_spot", {"scope": "continuous_opportunity_search"}),
        ("opportunity_reassessment_futures", {
            "scope": "opportunity_reassessment",
            "market_type": "FUTURES",
            "venue": "BINANCE_USDM_TESTNET",
        }),
        ("opportunity_reassessment_spot", {
            "scope": "opportunity_reassessment",
            "market_type": "SPOT",
            "venue": "BINANCE_SPOT_TESTNET",
        }),
    )
    for job_name, payload in disabled:
        scheduler.register_interval(
            job_name,
            interval_seconds=1800,
            first_run_at=first_run,
            payload=payload,
            enabled=False,
        )



def disable_spot_intelligence_schedules(scheduler: DurableJobScheduler) -> None:
    """Keep Spot research dormant until its separate Testnet scope is explicitly enabled."""
    first_run = datetime.now(timezone.utc) + timedelta(days=3650)
    disabled = (
        ("market_intelligence_scan_spot", {"scope": "continuous_opportunity_search"}),
        ("opportunity_reassessment_spot", {
            "scope": "opportunity_reassessment",
            "market_type": "SPOT",
            "venue": "BINANCE_SPOT_TESTNET",
        }),
    )
    for job_name, payload in disabled:
        scheduler.register_interval(
            job_name,
            interval_seconds=1800,
            first_run_at=first_run,
            payload=payload,
            enabled=False,
        )


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



def configured_strategy_research_candle_limit() -> int:
    """Require enough stateless 1m history for four validation folds plus untouched holdout."""
    return _bounded_env_int(
        "PAPER_STRATEGY_RESEARCH_CANDLES", 4500,
        minimum=4500, maximum=10000,
    )


def _bounded_env_int(
    name: str, default: int, *, minimum: int, maximum: int,
) -> int:
    """Read a bounded integer setting; reject invalid overrides instead of silently overloading the worker."""
    raw = os.environ.get(name)
    try:
        value = default if raw is None else int(raw.strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}") from exc
    if isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return value

def persist_equity_snapshot(ledger, *, initial_equity: float, as_of: float,
                            max_mark_age_seconds: float):
    """Persist a durable mark-to-market snapshot without granting order authority."""
    snapshot = ledger.mark_to_market_risk_report(
        initial_equity=initial_equity,
        as_of=as_of,
        max_mark_age_seconds=max_mark_age_seconds,
    )
    if getattr(snapshot, "orders_authorized", True) is not False:
        raise RuntimeError("equity snapshot unexpectedly authorized exchange orders")
    logger.info(
        "paper equity snapshot persisted equity=%s marks_fresh=%s open_positions=%s orders_authorized=False",
        snapshot.account_equity, snapshot.marks_fresh, snapshot.open_position_count,
    )
    return snapshot


def opportunity_reassessment_handler(coordinator: OpportunityCoordinator, source, *, paper_service=None, allow_paper_handoff: bool = False):
    """Build a venue-bound reassessment job using only public price observations."""
    if not isinstance(coordinator, OpportunityCoordinator):
        raise ValueError("a venue-scoped OpportunityCoordinator is required")
    if not callable(getattr(source, "fetch_mark_price", None)):
        raise ValueError("market source must provide read-only fetch_mark_price")
    if not isinstance(allow_paper_handoff, bool):
        raise ValueError("allow_paper_handoff must be an explicit boolean")

    def handle(payload: dict, idempotency_key: str) -> None:
        if payload.get("scope") != "opportunity_reassessment":
            raise ValueError("opportunity reassessment requires scope=opportunity_reassessment")
        if payload.get("market_type") != coordinator.market_type or payload.get("venue") != coordinator.venue:
            raise ValueError("reassessment job venue does not match its isolated coordinator")
        now = datetime.now(timezone.utc)

        def update(symbol: str, timeframe: str, stamp: datetime) -> MarketTick:
            snapshot = source.fetch_mark_price(symbol)
            if (
                getattr(snapshot, "symbol", None) != symbol
                or getattr(snapshot, "orders_authorized", None) is not False
                or getattr(snapshot, "market_type", coordinator.market_type) != coordinator.market_type
            ):
                raise ValueError("market source returned an invalid or mismatched paper-only price snapshot")
            price = getattr(snapshot, "mark_price", None)
            observed_at = getattr(snapshot, "observed_at", None)
            candle_rows = source.fetch_klines(
                symbol, interval=timeframe, limit=2, now=stamp,
            )
            closed = [row for row in candle_rows if getattr(row, "is_closed", False)]
            if not closed:
                raise ValueError("no closed candle is available for the opportunity timeframe")
            latest_closed = closed[-1]
            close_at = getattr(latest_closed, "close_time", None)
            if not isinstance(close_at, datetime) or close_at.tzinfo is None or close_at.utcoffset() is None:
                raise ValueError("latest closed candle timestamp is invalid")
            age_seconds = (stamp - close_at.astimezone(timezone.utc)).total_seconds()
            max_age_seconds = {
                "1m": 180, "5m": 600, "15m": 1800, "30m": 3600,
                "1h": 7200, "4h": 28800, "1d": 172800,
                "1w": 1209600, "1M": 2678400,
            }.get(timeframe)
            if max_age_seconds is None or age_seconds < -2 or age_seconds > max_age_seconds:
                raise ValueError("latest closed candle is stale or timeframe is unsupported")
            return MarketTick(
                symbol=symbol, price=price, observed_at=observed_at,
                candle_close=getattr(latest_closed, "close", None),
            )

        result = coordinator.reassess_due(
            now=now, market_update_provider=update, limit=20,
        )
        if result.orders_authorized is not False:
            raise RuntimeError("opportunity coordinator violated paper-only authorization boundary")

        # A trigger is only a research state. Futures must be freshly revalidated
        # and passed through the existing PaperSignalCycle/Risk Guardian boundary;
        # Spot remains research-only until it has an isolated Spot paper runtime.
        handoff_count = 0
        reconciliation_count = 0
        if coordinator.market_type == "FUTURES" and paper_service is not None:
            from backend.opportunity_handoff import (
                process_triggered_opportunity, reconcile_paper_intents,
            )
            if allow_paper_handoff:
                page = coordinator.store.list_opportunities(
                    states=("TRIGGERED", "RISK_CHECK"),
                    market_type=coordinator.market_type,
                    venue=coordinator.venue,
                    limit=20,
                )
                if page.next_cursor is not None:
                    raise RuntimeError("trigger handoff exceeded its bounded opportunity page")
                for opportunity in page.items:
                    handoff = process_triggered_opportunity(
                        coordinator, opportunity, source=source,
                        paper_service=paper_service, now=now,
                    )
                    if handoff.get("orders_authorized") is not False:
                        raise RuntimeError("opportunity handoff violated paper-only authorization boundary")
                    handoff_count += 1
            # Ledger reconciliation is read-only with respect to new intents; it only
            # records states already confirmed by the durable paper ledger.
            reconciled = reconcile_paper_intents(
                coordinator, paper_service.ledger, now=now,
            )
            if any(item.get("orders_authorized") is not False for item in reconciled):
                raise RuntimeError("paper opportunity reconciliation violated paper-only authorization boundary")
            reconciliation_count = len(reconciled)

        logger.info(
            "opportunity reassessment completed venue=%s market_type=%s evaluated=%d failed=%d handoffs=%d reconciled=%d job=%s orders_authorized=False",
            coordinator.venue, coordinator.market_type, result.evaluated_count,
            result.failed_count, handoff_count, reconciliation_count, idempotency_key,
        )

    return handle


def _safe_dispatch_error_label(value: Any) -> str | None:
    """Persist only a safe exception-type label, never arbitrary dispatcher text."""
    if value is None:
        return None
    if not isinstance(value, str):
        return type(value).__name__[:80]
    label = value.partition(":")[0].strip()
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,79}", label):
        return label
    return "DispatchError"


def run_scheduled_jobs(
    stop: threading.Event, scheduler: DurableJobScheduler,
    dispatcher: AnalysisJobDispatcher, *, poll_interval: float = 5.0,
    health: dict | None = None, health_lock=None,
) -> None:
    """Run durable analysis jobs off the feed loop and publish scheduler health."""
    if not isinstance(stop, threading.Event):
        raise ValueError("stop must be a threading.Event")
    if not isinstance(poll_interval, (int, float)) or poll_interval <= 0:
        raise ValueError("poll_interval must be positive")
    if health is not None and not isinstance(health, dict):
        raise ValueError("health must be a dictionary when supplied")
    scheduler_error = None

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
        try:
            enqueued = scheduler.enqueue_due(coalesce_missed=True)
            dispatcher.run_once()
            scheduler_error = None
        except Exception as exc:
            scheduler_error = type(exc).__name__
            logger.exception("durable read-only analysis scheduler thread failed")
            enqueued = 0
        dispatch_status = getattr(dispatcher, "last_run_status", "IDLE")
        dispatch_error = getattr(dispatcher, "last_error", None)
        degraded = scheduler_error is not None or dispatch_error is not None or dispatch_status == "LEASE_LOST"
        safe_dispatch_error = _safe_dispatch_error_label(dispatch_error)
        publish_health(
            status="DEGRADED" if degraded else "RUNNING",
            last_poll_at=datetime.now(timezone.utc).isoformat(),
            last_error=scheduler_error or safe_dispatch_error,
            last_dispatch_status=dispatch_status,
            jobs_enqueued=enqueued,
        )
        stop.wait(float(poll_interval))



def scheduled_job_runner_stopped_unexpectedly(job_runner, stop: threading.Event) -> bool:
    """Distinguish a crashed scheduler thread from the normal SIGTERM shutdown path."""
    return job_runner is not None and not stop.is_set() and not job_runner.is_alive()


def _shutdown_worker(
    stop: threading.Event, job_runner, store, app, *, grace_seconds: float,
) -> bool:
    """Stop background jobs before releasing worker ownership or shared resources.

    If a job thread outlives its grace period, leave the PostgreSQL worker lease
    and ledger connection untouched. The process is expected to exit; the lease
    then expires naturally, preventing a second worker from overlapping a still
    active job handler in the old process.
    """
    if not isinstance(stop, threading.Event):
        raise ValueError("stop must be a threading.Event")
    if (
        isinstance(grace_seconds, bool)
        or not isinstance(grace_seconds, (int, float))
        or not math.isfinite(float(grace_seconds))
        or grace_seconds <= 0
    ):
        raise ValueError("grace_seconds must be a finite positive number")

    stop.set()
    if job_runner is not None:
        if job_runner.is_alive():
            job_runner.join(timeout=float(grace_seconds))
        if job_runner.is_alive():
            logger.error(
                "scheduled-job runner remains active after shutdown grace; "
                "preserving worker lease and shared ledger until process exit"
            )
            return False

    try:
        store.release()
    finally:
        if app is not None:
            app.close()
    return True

def acquire_worker_lease_when_available(store, stop: threading.Event, *, retry_seconds: float = 5.0) -> bool:
    """Wait for the active worker to release its lease during a rolling deployment.

    Render can briefly run old and new instances together. A valid lease held by
    the old instance is not a fatal startup error; the new worker waits without
    polling until it can acquire the lease. Shutdown interrupts the wait.
    """
    if (
        isinstance(retry_seconds, bool)
        or not isinstance(retry_seconds, (int, float))
        or not math.isfinite(float(retry_seconds))
        or retry_seconds <= 0
    ):
        raise ValueError("retry_seconds must be a finite positive number")
    while not stop.is_set():
        if store.acquire_lease():
            logger.info("acquired PostgreSQL market-worker lease")
            return True
        logger.info(
            "market-worker lease is held by another instance; waiting %.1f seconds before retry",
            float(retry_seconds),
        )
        if stop.wait(float(retry_seconds)):
            break
    return False


def configure_strategy_research_schedule(
    scheduler: DurableJobScheduler,
    *,
    enabled: bool,
    interval_seconds: int = 21600,
    now: datetime | None = None,
) -> bool:
    """Register the paper-only research job only after an explicit staging opt-in."""
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be an explicit boolean")
    if not enabled:
        return False
    if (
        isinstance(interval_seconds, bool)
        or not isinstance(interval_seconds, int)
        or not 3600 <= interval_seconds <= 86400
    ):
        raise ValueError("interval_seconds must be between 3600 and 86400")
    first_run = (now or datetime.now(timezone.utc)) + timedelta(seconds=60)
    scheduler.register_interval(
        "strategy_research_evaluation",
        interval_seconds=interval_seconds,
        first_run_at=first_run,
        payload={
            "scope": "strategy_research",
            "market_type": "FUTURES",
            "venue": "BINANCE_USDM_TESTNET",
        },
    )
    return True


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
    stop = threading.Event()

    def request_stop(signum, frame) -> None:
        logger.info("shutdown requested; stopping paper-only market worker")
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    if not acquire_worker_lease_when_available(store, stop):
        logger.info("worker shutdown requested before market lease acquisition; no lease released")
        return

    app = None
    job_runner = None
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
        intelligence_enabled = _env_bool("PAPER_MARKET_INTELLIGENCE_ENABLED", default=False)
        spot_intelligence_enabled = _env_bool("PAPER_SPOT_INTELLIGENCE_ENABLED", default=False)
        strategy_research_enabled = _env_bool("PAPER_STRATEGY_RESEARCH_ENABLED", default=False)
        paper_handoff_enabled = _env_bool("PAPER_OPPORTUNITY_PAPER_HANDOFF_ENABLED", default=False)
        research_interval = (
            _bounded_env_int(
                "PAPER_STRATEGY_RESEARCH_INTERVAL_SECONDS", 21600,
                minimum=3600, maximum=86400,
            ) if strategy_research_enabled else 21600
        )
        configure_strategy_research_schedule(
            scheduler, enabled=strategy_research_enabled,
            interval_seconds=research_interval,
        )
        if intelligence_enabled:
            intelligence_interval = _bounded_env_int(
                "PAPER_MARKET_INTELLIGENCE_INTERVAL_SECONDS", 1800,
                minimum=900, maximum=86400,
            )
            scheduler.register_interval(
                "market_intelligence_scan",
                interval_seconds=intelligence_interval,
                first_run_at=datetime.now(timezone.utc) + timedelta(seconds=60),
                payload={"scope": "continuous_opportunity_search"},
            )
            opportunity_interval = _bounded_env_int(
                "PAPER_OPPORTUNITY_REASSESS_INTERVAL_SECONDS", 300,
                minimum=300, maximum=3600,
            )
            scheduler.register_interval(
                "opportunity_reassessment_futures",
                interval_seconds=opportunity_interval,
                first_run_at=datetime.now(timezone.utc) + timedelta(seconds=120),
                payload={
                    "scope": "opportunity_reassessment",
                    "market_type": "FUTURES",
                    "venue": "BINANCE_USDM_TESTNET",
                },
            )
            if spot_intelligence_enabled:
                scheduler.register_interval(
                    "opportunity_reassessment_spot",
                    interval_seconds=opportunity_interval,
                    first_run_at=datetime.now(timezone.utc) + timedelta(seconds=150),
                    payload={
                        "scope": "opportunity_reassessment",
                        "market_type": "SPOT",
                        "venue": "BINANCE_SPOT_TESTNET",
                    },
                )
            else:
                disable_spot_intelligence_schedules(scheduler)
        # Acquire the durable lease before creating the paper runtime: create() performs
        # restart recovery and must never run in a duplicate worker process.
        app = PaperServiceApplication.from_env()
        testnet = str(os.environ.get("PAPER_MARKET_TESTNET", "true")).strip().lower() in {"1", "true", "yes"}
        archive_enabled = _env_bool("PAPER_CANDLE_ARCHIVE_ENABLED", default=False)
        archive = None
        if archive_enabled:
            if not testnet:
                raise SystemExit("historical candle archive is Testnet-only; refusing production market data")
            if app.config.candle_interval != "1m":
                raise SystemExit("historical candle archive requires PAPER_CANDLE_INTERVAL=1m")
            archive = PostgresCandleArchive(dsn)
            logger.warning("permanent candle archive explicitly enabled; PAPER_CANDLE_ARCHIVE_ENABLED=true")
        else:
            logger.info("stateless candle mode enabled; raw candles are fetched on demand and are not persisted")
        intelligence_handlers = {"market_feed_health_audit": market_feed_health_audit(store)}
        if strategy_research_enabled:
            from backend.strategy_research_memory import PostgresStrategyResearchMemory
            from backend.strategy_research_runtime import build_strategy_research_handler

            research_candle_limit = configured_strategy_research_candle_limit()
            research_memory = PostgresStrategyResearchMemory(dsn)
            intelligence_handlers["strategy_research_evaluation"] = build_strategy_research_handler(
                archive, research_memory,
                source=app.worker.source,
                symbols=app.config.symbols_list,
                candles_per_symbol=research_candle_limit,
                venue="BINANCE_USDM_TESTNET",
                market_type="FUTURES",
            )
            logger.info(
                "paper-only walk-forward research schedule enabled interval_seconds=%d candles_per_symbol=%d orders_authorized=False",
                research_interval, research_candle_limit,
            )
        else:
            logger.info("paper-only walk-forward research schedule disabled by default orders_authorized=False")
        if intelligence_enabled:
            from backend.continuous_opportunity_search import continuous_opportunity_search

            intelligence_candle_limit = _bounded_env_int(
                "PAPER_MARKET_INTELLIGENCE_CANDLE_LIMIT", 100,
                minimum=30, maximum=500,
            )
            intelligence_fetch_workers = _bounded_env_int(
                "PAPER_MARKET_INTELLIGENCE_FETCH_WORKERS", 3,
                minimum=1, maximum=6,
            )
            intelligence_interval = _bounded_env_int(
                "PAPER_MARKET_INTELLIGENCE_INTERVAL_SECONDS", 1800,
                minimum=900, maximum=86400,
            )
            opportunity_store = PostgresOpportunityStore(dsn)
            futures_coordinator = OpportunityCoordinator(
                opportunity_store,
                owner=f"{worker_id[:72]}-futures-opportunities",
                market_type="FUTURES", venue="BINANCE_USDM_TESTNET",
            )
            intelligence_handlers["market_intelligence_scan"] = continuous_opportunity_search(
                store, app.worker.source, app.config.symbols,
                market_type="FUTURES", venue="BINANCE_USDM_TESTNET",
                candle_limit=intelligence_candle_limit,
                max_fetch_workers=intelligence_fetch_workers,
                opportunity_coordinator=futures_coordinator,
            )
            intelligence_handlers["opportunity_reassessment_futures"] = opportunity_reassessment_handler(
                futures_coordinator, app.worker.source, paper_service=app,
                allow_paper_handoff=paper_handoff_enabled,
            )
            if spot_intelligence_enabled:
                spot_coordinator = OpportunityCoordinator(
                    opportunity_store,
                    owner=f"{worker_id[:72]}-spot-opportunities",
                    market_type="SPOT", venue="BINANCE_SPOT_TESTNET",
                )
                spot_source = BinanceSpotMarketDataAdapter()
                scheduler.register_interval(
                    "market_intelligence_scan_spot",
                    interval_seconds=intelligence_interval,
                    first_run_at=datetime.now(timezone.utc) + timedelta(seconds=90),
                    payload={"scope": "continuous_opportunity_search"},
                )
                intelligence_handlers["market_intelligence_scan_spot"] = continuous_opportunity_search(
                    store, spot_source, app.config.symbols,
                    market_type="SPOT", venue="BINANCE_SPOT_TESTNET",
                    candle_limit=intelligence_candle_limit,
                    max_fetch_workers=intelligence_fetch_workers,
                    opportunity_coordinator=spot_coordinator,
                )
                intelligence_handlers["opportunity_reassessment_spot"] = opportunity_reassessment_handler(
                    spot_coordinator, spot_source,
                )
        else:
            disable_optional_intelligence_schedules(scheduler)
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
        equity_snapshot_interval_seconds = max(
            5.0, float(os.environ.get("PAPER_EQUITY_SNAPSHOT_INTERVAL_SECONDS", "60"))
        )
        last_equity_snapshot_at = 0.0

        scheduler_poll_interval = _bounded_env_int(
            "PAPER_SCHEDULED_JOB_POLL_INTERVAL_SECONDS", 5,
            minimum=3, maximum=30,
        )
        scheduler_health = {
            "status": "STARTING", "last_poll_started_at": None, "last_poll_at": None,
            "last_error": None, "last_dispatch_status": "IDLE",
            "jobs_enqueued": 0,
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
        process_started_at = datetime.now(timezone.utc).isoformat()

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
                equity_snapshot_error = None
                snapshot_clock = time.monotonic()
                if snapshot_clock - last_equity_snapshot_at >= equity_snapshot_interval_seconds:
                    try:
                        persist_equity_snapshot(
                            app.ledger,
                            initial_equity=app.config.simulated_equity,
                            as_of=time.time(),
                            max_mark_age_seconds=app.config.max_market_age_seconds,
                        )
                    except Exception as snapshot_exc:
                        equity_snapshot_error = type(snapshot_exc).__name__
                        logger.exception(
                            "paper equity snapshot persistence failed; dashboard freshness remains unknown"
                        )
                    else:
                        last_equity_snapshot_at = snapshot_clock
                with scheduler_health_lock:
                    scheduler_health_snapshot = dict(scheduler_health)
                heartbeat_status = "DEGRADED" if (
                    equity_snapshot_error or analysis_error
                    or scheduler_health_snapshot.get("status") == "DEGRADED"
                    or cycle_status in {
                        "SKIPPED_UNHEALTHY_MARKET_DATA", "FUNDING_SYNC_DEGRADED",
                        "MARKET_UNIVERSE_STALE", "NO_HEALTHY_ALLOWLISTED_SYMBOLS",
                        "NO_ELIGIBLE_SYMBOLS", "ANALYZED_WITH_EXCLUSIONS",
                        "FUNDING_SYNC_ERROR", "MARKET_UNIVERSE_UNAVAILABLE",
                        "MARKET_UNIVERSE_REJECTED", "AUTHORIZATION_FLAG_REJECTED",
                        "ANALYSIS_RESULT_MISSING", "ANALYSIS_ELIGIBILITY_INVALID",
                    }
                ) else "RUNNING"
                store.heartbeat(
                    heartbeat_status,
                    details={
                        "process_id": os.getpid(),
                        "process_started_at": process_started_at,
                        "last_cycle_started_at": cycle_started_at,
                        "last_cycle_at": datetime.now(timezone.utc).isoformat(),
                        "cycle_duration_seconds": round(time.monotonic() - cycle_started_monotonic, 3),
                        "analysis_status": cycle_status,
                        "analysis_error": analysis_error,
                        "healthy_symbols": list(report.healthy_symbols),
                        "unhealthy_symbols": list(report.unhealthy_symbols),
                        "archived_new_candles": archived_count,
                        "historical_archive_caught_up": archive_caught_up,
                        "candle_archive_enabled": archive is not None,
                        "historical_archive_lookback_days": archive_lookback_days,
                        "historical_archive_older_candles": older_history_count,
                        "historical_archive_backfill_errors": older_history_errors,
                        "equity_snapshot_error": equity_snapshot_error,
                        "scheduled_jobs": scheduler_health_snapshot,
                        "orders_authorized": False,
                    },
                )
                logger.info(
                    "shared market cycle status=%s healthy=%s unhealthy=%s open_positions=%d archived_new_candles=%d archive_caught_up=%s candle_archive_enabled=%s orders_authorized=False",
                    report.analysis_status, report.healthy_symbols, report.unhealthy_symbols, report.open_positions, archived_count, archive_caught_up, archive is not None,
                )
                if scheduled_job_runner_stopped_unexpectedly(job_runner, stop):
                    raise RuntimeError("durable scheduled-job runner stopped unexpectedly")
                delay = app.worker.next_delay_seconds
            except Exception as exc:
                logger.exception("shared market worker cycle failed; feed remains fail-closed")
                try:
                    with scheduler_health_lock:
                        scheduler_health_snapshot = dict(scheduler_health)
                    store.heartbeat("DEGRADED", details={
                        "process_id": os.getpid(),
                        "process_started_at": process_started_at,
                        "last_cycle_started_at": cycle_started_at,
                        "last_cycle_at": datetime.now(timezone.utc).isoformat(),
                        "cycle_duration_seconds": round(time.monotonic() - cycle_started_monotonic, 3),
                        "analysis_status": "ANALYSIS_FAILED:WorkerCycleError",
                        "analysis_error": type(exc).__name__,
                        "scheduled_jobs": scheduler_health_snapshot,
                        "orders_authorized": False,
                    })
                except Exception:
                    logger.exception("market worker heartbeat failed; worker lease may be lost")
                    stop.set()
                delay = 15.0
            stop.wait(delay)
    finally:
        shutdown_grace = max(5.0, float(os.environ.get("PAPER_SCHEDULED_JOB_SHUTDOWN_GRACE_SECONDS", "15")))
        _shutdown_worker(
            stop, job_runner, store, app, grace_seconds=shutdown_grace,
        )


if __name__ == "__main__":
    main()
