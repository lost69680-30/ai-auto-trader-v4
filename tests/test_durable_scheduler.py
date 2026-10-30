from datetime import datetime, timedelta, timezone
import os

import pytest
import psycopg

from backend.durable_scheduler import DurableJobScheduler


DSN = os.environ.get("PAPER_TEST_DATABASE_URL")


def _schema_available():
    if not DSN:
        return False
    try:
        with psycopg.connect(DSN) as conn:
            row = conn.execute("SELECT to_regclass('paper_ledger.scheduled_jobs')").fetchone()
        return row is not None and row[0] is not None
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _schema_available(), reason="durable scheduler migration is not applied to this test database"
)


def reset_tables():
    with psycopg.connect(DSN) as conn:
        conn.execute("TRUNCATE paper_ledger.scheduled_jobs, paper_ledger.scheduled_job_definitions CASCADE")


def test_recurring_schedule_enqueues_each_due_occurrence_once():
    reset_tables()
    now = datetime.now(timezone.utc)
    scheduler = DurableJobScheduler(DSN)
    scheduler.register_interval(
        "daily_research", interval_seconds=60,
        first_run_at=now - timedelta(seconds=120), payload={"scope": "paper"},
    )
    assert scheduler.enqueue_due(now=now) == 3
    assert scheduler.enqueue_due(now=now) == 0


def test_job_claim_failure_retry_and_completion_are_idempotent():
    reset_tables()
    now = datetime.now(timezone.utc)
    scheduler = DurableJobScheduler(DSN, lease_seconds=30)
    scheduler.register_interval("market_review", interval_seconds=3600, first_run_at=now)
    assert scheduler.enqueue_due(now=now) == 1
    job = scheduler.claim_next(owner="worker-a", now=now)
    assert job is not None
    assert job["status"] == "RUNNING"
    assert job["orders_authorized"] is False
    assert scheduler.fail(job["idempotency_key"], owner="worker-a", error="temporary error", now=now) == "PENDING"
    retry = scheduler.claim_next(owner="worker-b", now=now + timedelta(seconds=1))
    assert retry["idempotency_key"] == job["idempotency_key"]
    assert retry["attempts"] == 2
    assert scheduler.complete(retry["idempotency_key"], owner="worker-b", now=now + timedelta(seconds=2)) is True
    assert scheduler.complete(retry["idempotency_key"], owner="worker-b", now=now + timedelta(seconds=3)) is False


def test_expired_job_lease_can_be_reclaimed_but_old_owner_cannot_complete():
    reset_tables()
    now = datetime.now(timezone.utc)
    scheduler = DurableJobScheduler(DSN, lease_seconds=10)
    scheduler.register_interval("recovery_check", interval_seconds=3600, first_run_at=now)
    scheduler.enqueue_due(now=now)
    first = scheduler.claim_next(owner="worker-a", now=now)
    second = scheduler.claim_next(owner="worker-b", now=now + timedelta(seconds=11))
    assert second["idempotency_key"] == first["idempotency_key"]
    assert second["attempts"] == 2
    assert scheduler.complete(first["idempotency_key"], owner="worker-a", now=now + timedelta(seconds=12)) is False
    assert scheduler.complete(second["idempotency_key"], owner="worker-b", now=now + timedelta(seconds=12)) is True


def test_scheduler_rejects_naive_schedule_time_and_invalid_intervals():
    scheduler = DurableJobScheduler(DSN)
    with pytest.raises(ValueError, match="timezone-aware"):
        scheduler.register_interval("bad_schedule", interval_seconds=60, first_run_at=datetime.now())
    with pytest.raises(ValueError, match="positive integer"):
        scheduler.register_interval("bad_schedule", interval_seconds=0, first_run_at=datetime.now(timezone.utc))
