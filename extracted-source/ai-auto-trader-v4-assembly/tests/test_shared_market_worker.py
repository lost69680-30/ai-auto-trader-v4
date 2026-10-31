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
