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


def test_reregistering_schedule_preserves_persisted_next_run_time():
    reset_tables()
    now = datetime.now(timezone.utc)
    scheduler = DurableJobScheduler(DSN)
    scheduler.register_interval(
        "daily_research", interval_seconds=60,
        first_run_at=now + timedelta(minutes=10), payload={"scope": "paper"},
    )
    with psycopg.connect(DSN) as conn:
        before = conn.execute(
            "SELECT next_run_at FROM paper_ledger.scheduled_job_definitions WHERE job_name = %s",
            ("daily_research",),
        ).fetchone()[0]

    # A process restart re-registers the same schedule. The supplied bootstrap
    # timestamp must not overwrite the durable cursor and postpone due work.
    scheduler.register_interval(
        "daily_research", interval_seconds=60,
        first_run_at=now + timedelta(days=1), payload={"scope": "paper"},
    )
    with psycopg.connect(DSN) as conn:
        after = conn.execute(
            "SELECT next_run_at FROM paper_ledger.scheduled_job_definitions WHERE job_name = %s",
            ("daily_research",),
        ).fetchone()[0]

    assert after == before


def test_reenabling_disabled_schedule_resets_its_next_run_cursor():
    reset_tables()
    now = datetime.now(timezone.utc)
    scheduler = DurableJobScheduler(DSN)
    scheduler.register_interval(
        "market_intelligence_scan", interval_seconds=1800,
        first_run_at=now, payload={"scope": "continuous_opportunity_search"},
    )
    scheduler.register_interval(
        "market_intelligence_scan", interval_seconds=1800,
        first_run_at=now + timedelta(days=3650),
        payload={"scope": "disabled"}, enabled=False,
    )
    next_run = now + timedelta(seconds=60)
    scheduler.register_interval(
        "market_intelligence_scan", interval_seconds=1800,
        first_run_at=next_run, payload={"scope": "continuous_opportunity_search"},
        enabled=True,
    )

    with psycopg.connect(DSN) as conn:
        enabled, persisted_next_run = conn.execute(
            "SELECT enabled, next_run_at FROM paper_ledger.scheduled_job_definitions WHERE job_name = %s",
            ("market_intelligence_scan",),
        ).fetchone()

    assert enabled is True
    assert persisted_next_run == next_run

def test_claim_next_only_claims_explicitly_enabled_job_names():
    reset_tables()
    now = datetime.now(timezone.utc)
    scheduler = DurableJobScheduler(DSN, lease_seconds=30)
    scheduler.register_interval("disabled_market_scan", interval_seconds=3600, first_run_at=now)
    scheduler.register_interval("market_feed_health_audit", interval_seconds=3600, first_run_at=now)
    scheduler.enqueue_due(now=now)

    job = scheduler.claim_next(
        owner="worker-a", now=now,
        allowed_job_names={"market_feed_health_audit"},
    )

    assert job is not None
    assert job["job_name"] == "market_feed_health_audit"
    with psycopg.connect(DSN) as conn:
        unclaimed = conn.execute(
            "SELECT status FROM paper_ledger.scheduled_jobs WHERE job_name = %s",
            ("disabled_market_scan",),
        ).fetchone()
    assert unclaimed == ("PENDING",)

def test_coalescing_missed_intervals_enqueues_only_latest_due_occurrence():
    reset_tables()
    scheduler = DurableJobScheduler(DSN)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    first = now - timedelta(minutes=3)
    scheduler.register_interval(
        "market_intelligence_scan", interval_seconds=60,
        first_run_at=first, payload={"scope": "continuous_opportunity_search"},
    )

    assert scheduler.enqueue_due(now=now, coalesce_missed=True) == 1
    assert scheduler.enqueue_due(now=now, coalesce_missed=True) == 0

    with psycopg.connect(DSN) as conn:
        next_run = conn.execute(
            "SELECT next_run_at FROM paper_ledger.scheduled_job_definitions WHERE job_name = %s",
            ("market_intelligence_scan",),
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT scheduled_for, status FROM paper_ledger.scheduled_jobs WHERE job_name = %s ORDER BY scheduled_for",
            ("market_intelligence_scan",),
        ).fetchall()

    assert next_run == now + timedelta(seconds=60)
    assert rows == [(now, "PENDING")]
