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
from backend.market_data_adapter import BinanceSpotMarketDataAdapter
from backend.opportunity_coordinator import OpportunityCoordinator
from backend.opportunity_store import PostgresOpportunityStore
from backend.signal_watcher import MarketTick
from backend.historical_candle_archive import PostgresCandleArchive, archive_is_caught_up, archive_market_poll, backfill_older_symbol_history, backfill_symbol_history
from backend.shared_market_store import PostgresMarketFeedStore


logger = logging.getLogger(__name__)


def opportunity_reassessment_handler(coordinator: OpportunityCoordinator, source, *, paper_service=None):
    """Build a venue-bound reassessment job using only public price observations."""
    if not isinstance(coordinator, OpportunityCoordinator):
        raise ValueError("a venue-scoped OpportunityCoordinator is required")
    if not callable(getattr(source, "fetch_mark_price", None)):
        raise ValueError("market source must provide read-only fetch_mark_price")

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
                or getattr(snapshot, "market_type", None) != coordinator.market_type
            ):
                raise ValueError("market source returned an invalid or mismatched paper-only price snapshot")
            price = getattr(snapshot, "mark_price", None)
            observed_at = getattr(snapshot, "observed_at", None)
            if (
                isinstance(price, bool) or not isinstance(price, (int, float))
                or not math.isfinite(price) or price <= 0
            ):
                raise ValueError("market source returned an invalid mark price")
            if not isinstance(observed_at, datetime) or observed_at.tzinfo is None or observed_at.utcoffset() is None:
                raise ValueError("market source returned an invalid mark observation timestamp")
            mark_age = (stamp - observed_at.astimezone(timezone.utc)).total_seconds()
            if mark_age < -2 or mark_age > 30:
                raise ValueError("market source mark price is stale or future-dated")
            candle_rows = source.fetch_klines(
                symbol, interval=timeframe, limit=2, now=stamp,
            )
            closed = [row for row in candle_rows if getattr(row, "is_closed", False)]
            if not closed:
                raise ValueError("no closed candle is available for the opportunity timeframe")
            latest_closed = closed[-1]
            close_at = getattr(latest_closed, "close_time", None)
            close_price = getattr(latest_closed, "close", None)
            if (
                getattr(latest_closed, "symbol", symbol) != symbol
                or isinstance(close_price, bool)
                or not isinstance(close_price, (int, float))
                or not math.isfinite(close_price)
                or close_price <= 0
            ):
                raise ValueError("latest closed candle does not match the requested symbol or price")
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
            now=now, market_update_provider=update, limit=50,
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
            page = coordinator.store.list_opportunities(
                states=("TRIGGERED", "RISK_CHECK"),
                market_type=coordinator.market_type,
                venue=coordinator.venue,
                limit=50,
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


def run_scheduled_jobs(stop: threading.Event, scheduler: DurableJobScheduler,
                       dispatcher: AnalysisJobDispatcher, *, poll_interval: float = 1.0) -> None:
    """Run durable analysis jobs off the market-feed polling thread."""
    if not isinstance(stop, threading.Event):
        raise ValueError("stop must be a threading.Event")
    if not isinstance(poll_interval, (int, float)) or poll_interval <= 0:
        raise ValueError("poll_interval must be positive")
    while not stop.is_set():
        try:
            scheduler.enqueue_due()
            dispatcher.run_once()
        except Exception:
            logger.exception("durable read-only analysis scheduler thread failed")
        stop.wait(float(poll_interval))


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
    stop = threading.Event()

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
                payload={"scope": "continuous_opportunity_search"},
            )
            opportunity_interval = max(
                15, int(os.environ.get("PAPER_OPPORTUNITY_REASSESS_INTERVAL_SECONDS", "60"))
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
        intelligence_handlers = {"market_feed_health_audit": market_feed_health_audit(store)}
        if intelligence_enabled:
            from backend.continuous_opportunity_search import continuous_opportunity_search

            intelligence_candle_limit = int(
                os.environ.get("PAPER_MARKET_INTELLIGENCE_CANDLE_LIMIT", "200")
            )
            intelligence_fetch_workers = int(
                os.environ.get("PAPER_MARKET_INTELLIGENCE_FETCH_WORKERS", "12")
            )
            intelligence_interval = max(
                300, int(os.environ.get("PAPER_MARKET_INTELLIGENCE_INTERVAL_SECONDS", "900"))
            )
            opportunity_store = PostgresOpportunityStore(dsn)
            futures_coordinator = OpportunityCoordinator(
                opportunity_store,
                owner=f"{worker_id[:72]}-futures-opportunities",
                market_type="FUTURES", venue="BINANCE_USDM_TESTNET",
            )
            spot_coordinator = OpportunityCoordinator(
                opportunity_store,
                owner=f"{worker_id[:72]}-spot-opportunities",
                market_type="SPOT", venue="BINANCE_SPOT_TESTNET",
            )
            intelligence_handlers["market_intelligence_scan"] = continuous_opportunity_search(
                store, app.worker.source, app.config.symbols,
                market_type="FUTURES", venue="BINANCE_USDM_TESTNET",
                candle_limit=intelligence_candle_limit,
                max_fetch_workers=intelligence_fetch_workers,
                opportunity_coordinator=futures_coordinator,
            )
            # Spot uses its own public Testnet adapter and scan identity. It does
            # not share Futures mark/funding semantics or the paper runtime's
            # market source, and it has no exchange-order interface.
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
            intelligence_handlers["opportunity_reassessment_futures"] = opportunity_reassessment_handler(
                futures_coordinator, app.worker.source, paper_service=app,
            )
            intelligence_handlers["opportunity_reassessment_spot"] = opportunity_reassessment_handler(
                spot_coordinator, spot_source,
            )
        dispatcher = AnalysisJobDispatcher(
            scheduler,
            owner=os.environ.get("PAPER_ANALYSIS_DISPATCHER_ID", f"{worker_id[:110]}-analysis"),
            handlers=intelligence_handlers,
        )
        archive_lookback_days = int(os.environ.get("PAPER_MARKET_ARCHIVE_LOOKBACK_DAYS", "30"))
        if not 1 <= archive_lookback_days <= 365:
            raise SystemExit("PAPER_MARKET_ARCHIVE_LOOKBACK_DAYS must be between 1 and 365")

        job_runner = threading.Thread(
            target=run_scheduled_jobs,
            args=(stop, scheduler, dispatcher),
            name=f"{worker_id[:80]}-scheduled-jobs",
            daemon=True,
        )
        job_runner.start()

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
                if job_runner is not None and not job_runner.is_alive():
                    raise RuntimeError("durable scheduled-job runner stopped unexpectedly")
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
        stop.set()
        if job_runner is not None:
            shutdown_grace = max(5.0, float(os.environ.get("PAPER_SCHEDULED_JOB_SHUTDOWN_GRACE_SECONDS", "15")))
            job_runner.join(timeout=shutdown_grace)
            if job_runner.is_alive():
                logger.warning("scheduled-job runner did not stop within shutdown grace; its durable job lease will expire for safe retry")
        try:
            store.release()
        finally:
            if app is not None:
                app.close()


if __name__ == "__main__":
    main()
