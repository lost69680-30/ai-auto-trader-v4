import pytest

from backend import shared_market_worker


class FakeStore:
    released = False

    def __init__(self, *args, **kwargs):
        pass

    def acquire_lease(self):
        return True

    def release(self):
        type(self).released = True


def test_scheduler_startup_failure_releases_market_worker_lease(monkeypatch):
    FakeStore.released = False
    monkeypatch.setenv("PAPER_DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(shared_market_worker, "PostgresMarketFeedStore", FakeStore)

    def fail_scheduler(*args, **kwargs):
        raise RuntimeError("scheduler database unavailable")

    monkeypatch.setattr(shared_market_worker, "DurableJobScheduler", fail_scheduler)

    with pytest.raises(RuntimeError, match="scheduler database unavailable"):
        shared_market_worker.main()

    assert FakeStore.released is True

def test_scheduled_job_runner_executes_off_feed_loop_and_stops_cleanly():
    import threading

    class Scheduler:
        def __init__(self):
            self.called = threading.Event()

        def enqueue_due(self):
            self.called.set()

    class Dispatcher:
        def __init__(self):
            self.called = threading.Event()

        def run_once(self):
            self.called.set()
            return True

    stop = threading.Event()
    scheduler = Scheduler()
    dispatcher = Dispatcher()
    thread = threading.Thread(
        target=shared_market_worker.run_scheduled_jobs,
        args=(stop, scheduler, dispatcher),
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    thread.start()
    assert scheduler.called.wait(1.0)
    assert dispatcher.called.wait(1.0)
    stop.set()
    thread.join(timeout=1.0)
    assert not thread.is_alive()


def test_opportunity_reassessment_handler_uses_venue_scoped_read_only_price_source():
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from backend.opportunity_coordinator import OpportunityCoordinator
    from backend.shared_market_worker import opportunity_reassessment_handler

    coordinator = object.__new__(OpportunityCoordinator)
    coordinator.market_type = "SPOT"
    coordinator.venue = "BINANCE_SPOT_TESTNET"
    seen = {}

    def reassess_due(*, now, market_update_provider, limit):
        seen["limit"] = limit
        seen["now"] = now
        tick = market_update_provider("BTCUSDT", now)
        seen["tick"] = tick
        return SimpleNamespace(evaluated_count=1, failed_count=0, orders_authorized=False)

    coordinator.reassess_due = reassess_due

    class SpotSource:
        def fetch_mark_price(self, symbol):
            return SimpleNamespace(
                symbol=symbol, mark_price=123.4,
                observed_at=datetime.now(timezone.utc),
                orders_authorized=False, market_type="SPOT",
            )

    handler = opportunity_reassessment_handler(coordinator, SpotSource())
    handler(
        {"scope": "opportunity_reassessment", "market_type": "SPOT",
         "venue": "BINANCE_SPOT_TESTNET"},
        "spot-reassessment-1",
    )

    assert seen["limit"] == 50
    assert seen["tick"].symbol == "BTCUSDT"
    assert seen["tick"].price == 123.4


def test_opportunity_reassessment_handler_rejects_cross_venue_job_payload():
    from backend.opportunity_coordinator import OpportunityCoordinator
    from backend.shared_market_worker import opportunity_reassessment_handler

    coordinator = object.__new__(OpportunityCoordinator)
    coordinator.market_type = "SPOT"
    coordinator.venue = "BINANCE_SPOT_TESTNET"
    coordinator.reassess_due = lambda **kwargs: None

    class SpotSource:
        def fetch_mark_price(self, symbol):
            raise AssertionError("cross-venue payload must be rejected before fetching")

    handler = opportunity_reassessment_handler(coordinator, SpotSource())
    with pytest.raises(ValueError, match="venue"):
        handler(
            {"scope": "opportunity_reassessment", "market_type": "FUTURES",
             "venue": "BINANCE_USDM_TESTNET"},
            "wrong-venue-job",
        )
