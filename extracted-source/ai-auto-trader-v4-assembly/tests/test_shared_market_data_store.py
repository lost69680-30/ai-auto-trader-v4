from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import os

import pytest

from backend.market_worker import FeedStatus, MarketPollResult
from backend.shared_market_store import PostgresMarketFeedStore


DSN = os.environ.get("PAPER_TEST_DATABASE_URL")


def _shared_schema_available():
    if not DSN:
        return False
    try:
        import psycopg
        with psycopg.connect(DSN) as conn:
            row = conn.execute("SELECT to_regclass('paper_ledger.market_feed_snapshots')").fetchone()
        return row is not None and row[0] is not None
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _shared_schema_available(),
    reason="shared market-data migration is not applied to this test database",
)


def reset_tables():
    import psycopg
    with psycopg.connect(DSN) as conn:
        conn.execute("TRUNCATE paper_ledger.market_feed_snapshots, paper_ledger.market_worker_heartbeats")


def healthy_poll(now):
    candle = SimpleNamespace(
        open_time=now - timedelta(minutes=1), close_time=now - timedelta(seconds=1),
        open=99.0, high=101.0, low=98.0, close=100.0, volume=12.0, is_closed=True,
    )
    frame = SimpleNamespace(
        symbol="BTCUSDT", status=FeedStatus.HEALTHY, checked_at=now,
        mark_price=100.0, mark_observed_at=now - timedelta(seconds=1),
        closed_candles=(candle,), reasons=(), retry_count=0,
        orders_authorized=False, funding_rate=0.0001, index_price=100.1,
        next_funding_at=now + timedelta(hours=8), market_type="FUTURES",
    )
    return MarketPollResult(now, (frame,), orders_authorized=False)


def test_postgres_store_persists_validated_snapshot_and_exposes_it_to_api():
    reset_tables()
    now = datetime.now(timezone.utc)
    store = PostgresMarketFeedStore(DSN, worker_id="test-market-worker", owner_id="owner-a")
    assert store.acquire_lease(now=now) is True
    store.publish(healthy_poll(now), now=now)
    status = store.status()
    assert status["status"] == "HEALTHY"
    assert status["worker_status"] == "RUNNING"
    items = store.items()
    assert len(items) == 1
    assert items[0]["symbol"] == "BTCUSDT"
    assert items[0]["price_type"] == "FUTURES_MARK_PRICE"
    assert items[0]["funding_rate"] == 0.0001
    assert items[0]["orders_authorized"] is False
    store.release(now=now)
    assert store.status()["status"] == "STALE"

    restarted = PostgresMarketFeedStore(DSN, worker_id="test-market-worker", owner_id="owner-b")
    assert restarted.acquire_lease() is True
    # Old snapshots remain durable, but are not exposed until the new worker validates a poll.
    assert restarted.status()["status"] == "STARTING"
    assert restarted.items() == []
    next_poll_at = datetime.now(timezone.utc)
    restarted.publish(healthy_poll(next_poll_at), now=next_poll_at)
    assert restarted.status()["status"] == "HEALTHY"
    restarted.release()


def test_postgres_store_prevents_second_worker_from_taking_live_lease():
    reset_tables()
    now = datetime.now(timezone.utc)
    first = PostgresMarketFeedStore(DSN, worker_id="test-market-worker", owner_id="owner-a")
    second = PostgresMarketFeedStore(DSN, worker_id="test-market-worker", owner_id="owner-b")
    assert first.acquire_lease(now=now) is True
    assert second.acquire_lease(now=now) is False
    first.release(now=now + timedelta(seconds=1))
    assert second.acquire_lease(now=now + timedelta(seconds=2)) is True
    second.release(now=now + timedelta(seconds=3))


def test_postgres_store_rejects_any_order_authorization_flag():
    reset_tables()
    now = datetime.now(timezone.utc)
    store = PostgresMarketFeedStore(DSN, worker_id="test-market-worker", owner_id="owner-a")
    assert store.acquire_lease(now=now) is True
    unsafe = SimpleNamespace(checked_at=now, frames=(), orders_authorized=True)
    with pytest.raises(ValueError, match="cannot authorize orders"):
        store.publish(unsafe, now=now)
    store.release(now=now + timedelta(seconds=1))



def test_postgres_store_rejects_healthy_futures_snapshot_without_funding_context():
    reset_tables()
    now = datetime.now(timezone.utc)
    store = PostgresMarketFeedStore(DSN, worker_id="test-market-worker", owner_id="owner-a")
    assert store.acquire_lease(now=now) is True
    valid = healthy_poll(now)
    bad_frame = SimpleNamespace(**vars(valid.frames[0]))
    bad_frame.funding_rate = None
    bad_poll = MarketPollResult(now, (bad_frame,), orders_authorized=False)
    with pytest.raises(ValueError, match="complete index and funding context"):
        store.publish(bad_poll, now=now)
    store.release()


def test_postgres_store_reports_missing_or_expired_worker_as_stale():
    reset_tables()
    store = PostgresMarketFeedStore(DSN, worker_id="test-market-worker", owner_id="owner-a")
    assert store.status()["status"] == "STARTING"
    now = datetime.now(timezone.utc)
    assert store.acquire_lease(now=now - timedelta(minutes=5)) is True
    assert store.status()["status"] == "STALE"
    store.release(now=now)
