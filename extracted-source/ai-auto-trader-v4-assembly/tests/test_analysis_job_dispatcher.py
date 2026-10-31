from datetime import datetime, timezone

import pytest

from backend.analysis_job_dispatcher import AnalysisJobDispatcher


class FakeScheduler:
    def __init__(self, jobs):
        self.jobs = list(jobs)
        self.completed = []
        self.failed = []

    def claim_next(self, *, owner):
        return self.jobs.pop(0) if self.jobs else None

    def complete(self, key, *, owner):
        self.completed.append((key, owner))
        return True

    def fail(self, key, *, owner, error):
        self.failed.append((key, owner, error))
        return "PENDING"


def test_dispatcher_runs_only_allowlisted_read_only_analysis_handler():
    called = []
    scheduler = FakeScheduler([{
        "idempotency_key": "market_feed_health_audit:1",
        "job_name": "market_feed_health_audit",
        "payload": {"scope": "market_feed"},
        "orders_authorized": False,
    }])
    dispatcher = AnalysisJobDispatcher(
        scheduler, owner="test-worker",
        handlers={"market_feed_health_audit": lambda payload, key: called.append((payload, key))},
    )

    assert dispatcher.run_once() is True
    assert called == [({"scope": "market_feed"}, "market_feed_health_audit:1")]
    assert scheduler.completed == [("market_feed_health_audit:1", "test-worker")]
    assert scheduler.failed == []


@pytest.mark.parametrize("job_name", [
    "place_order", "execute_trade", "create_fill", "change_risk_limits",
    "paper_order_execution", "unknown_task",
])
def test_dispatcher_rejects_execution_risk_and_unknown_job_types(job_name):
    scheduler = FakeScheduler([{
        "idempotency_key": f"{job_name}:1", "job_name": job_name,
        "payload": {}, "orders_authorized": False,
    }])
    dispatcher = AnalysisJobDispatcher(scheduler, owner="test-worker", handlers={})

    assert dispatcher.run_once() is False
    assert scheduler.completed == []
    assert len(scheduler.failed) == 1
    assert "not allowlisted" in scheduler.failed[0][2]


def test_dispatcher_fails_closed_if_job_claim_has_order_authority():
    scheduler = FakeScheduler([{
        "idempotency_key": "market_feed_health_audit:1",
        "job_name": "market_feed_health_audit",
        "payload": {}, "orders_authorized": True,
    }])
    dispatcher = AnalysisJobDispatcher(
        scheduler, owner="test-worker",
        handlers={"market_feed_health_audit": lambda payload, key: pytest.fail("must not run")},
    )

    assert dispatcher.run_once() is False
    assert scheduler.completed == []
    assert len(scheduler.failed) == 1
