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


def test_equity_snapshot_runner_uses_configured_baseline_and_stops_cleanly():
    import threading

    stop = threading.Event()
    calls = []

    class Ledger:
        def mark_to_market_risk_report(self, **kwargs):
            calls.append(kwargs)
            stop.set()
            return type("Report", (), {"marks_fresh": True, "account_equity": 10_000.0, "open_position_count": 0})()

    shared_market_worker.run_equity_snapshot_scheduler(
        stop, Ledger(), initial_equity=10_000.0,
        interval_seconds=60, max_mark_age_seconds=30,
        clock=lambda: 123.0,
    )

    assert calls == [{
        "initial_equity": 10_000.0,
        "as_of": 123.0,
        "max_mark_age_seconds": 30.0,
    }]


def test_equity_snapshot_runner_rejects_invalid_baseline_before_writing():
    import threading

    class Ledger:
        called = False

        def mark_to_market_risk_report(self, **kwargs):
            self.called = True

    ledger = Ledger()
    with pytest.raises(ValueError, match="initial_equity"):
        shared_market_worker.run_equity_snapshot_scheduler(
            threading.Event(), ledger, initial_equity=0,
            interval_seconds=60, max_mark_age_seconds=30,
            clock=lambda: 123.0,
        )
    assert ledger.called is False
