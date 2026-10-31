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
