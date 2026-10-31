from datetime import datetime, timedelta, timezone
import io
import json
from types import SimpleNamespace
import os
from pathlib import Path

import pytest

from backend.market_worker import FeedStatus, MarketPollResult
from backend.shared_market_store import PostgresMarketFeedStore
from app.engines.paper_ledger import SQLitePaperLedger
from app.engines.read_only_api import create_app


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
    # Earlier PostgreSQL ledger tests intentionally drop paper_ledger to verify
    # startup failure behavior. Reapply all idempotent migrations for isolation.
    import psycopg
    root = Path(__file__).resolve().parents[3]
    migrations = (
        root / "supabase/migrations/20261029205318_create_paper_ledger_schema.sql",
        root / "supabase/migrations/20261030195452_create_shared_market_data.sql",
        root / "supabase/migrations/20261030195505_create_durable_scheduled_jobs.sql",
        root / "supabase/migrations/20261031100000_create_market_intelligence_reports.sql",
    )
    with psycopg.connect(DSN, autocommit=True) as conn:
        for migration in migrations:
            conn.execute(migration.read_text(encoding="utf-8"), prepare=False)
        conn.execute("TRUNCATE paper_ledger.market_intelligence_reports, paper_ledger.market_feed_snapshots, paper_ledger.market_worker_heartbeats, paper_ledger.scheduled_jobs, paper_ledger.scheduled_job_definitions")


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
    store.heartbeat(
        "RUNNING",
        now=now,
        details={"analysis_status": "ANALYZED", "last_cycle_at": now.isoformat(), "orders_authorized": False},
    )
    assert store.analysis_status()["status"] == "RUNNING"
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



def _api_request(app, auth):
    captured = {}
    def start(status, headers):
        captured["status"] = status
    body = b"".join(app({
        "REQUEST_METHOD": "GET", "PATH_INFO": "/api/v1/markets", "QUERY_STRING": "",
        "HTTP_AUTHORIZATION": auth, "CONTENT_LENGTH": "0", "wsgi.input": io.BytesIO(b""),
    }, start))
    return captured["status"], json.loads(body)


def test_api_reads_worker_snapshots_and_fails_closed_across_worker_restart(tmp_path):
    reset_tables()
    now = datetime.now(timezone.utc)
    first = PostgresMarketFeedStore(DSN, worker_id="test-market-worker", owner_id="owner-a")
    assert first.acquire_lease(now=now) is True
    first.publish(healthy_poll(now), now=now)

    ledger_path = str(tmp_path / "api-ledger.sqlite3")
    with SQLitePaperLedger(ledger_path):
        pass
    api = create_app(
        database_path=ledger_path,
        token="shared-feed-api-test-token-at-least-32-chars",
        market_status=first.status,
        market_items=first.items,
    )
    status, payload = _api_request(api, "Bearer shared-feed-api-test-token-at-least-32-chars")
    assert status.startswith("200")
    assert payload["items"][0]["symbol"] == "BTCUSDT"

    first.release()
    status, payload = _api_request(api, "Bearer shared-feed-api-test-token-at-least-32-chars")
    assert status.startswith("503")
    assert payload["items"] == []

    restarted = PostgresMarketFeedStore(DSN, worker_id="test-market-worker", owner_id="owner-b")
    assert restarted.acquire_lease() is True
    status, payload = _api_request(api, "Bearer shared-feed-api-test-token-at-least-32-chars")
    assert status.startswith("503")
    assert payload["items"] == []

    fresh_at = datetime.now(timezone.utc)
    restarted.publish(healthy_poll(fresh_at), now=fresh_at)
    status, payload = _api_request(api, "Bearer shared-feed-api-test-token-at-least-32-chars")
    assert status.startswith("200")
    assert payload["items"][0]["symbol"] == "BTCUSDT"
    restarted.release()


def test_postgres_store_reports_missing_or_expired_worker_as_stale():
    reset_tables()
    store = PostgresMarketFeedStore(DSN, worker_id="test-market-worker", owner_id="owner-a")
    assert store.status()["status"] == "STARTING"
    now = datetime.now(timezone.utc)
    assert store.acquire_lease(now=now - timedelta(minutes=5)) is True
    assert store.status()["status"] == "STALE"
    store.release(now=now)

def test_market_intelligence_is_durable_and_survives_worker_heartbeat_updates():
    reset_tables()
    now = datetime.now(timezone.utc)
    store = PostgresMarketFeedStore(DSN, worker_id="test-intelligence-worker", owner_id="owner-intelligence")
    assert store.acquire_lease(now=now) is True
    store.heartbeat("RUNNING", now=now, details={"analysis_status": "RUNNING"})
    payload = {
        "scan_id": "scan-123",
        "assessed_at": now.isoformat(),
        "market_type": "FUTURES",
        "environment": "TESTNET",
        "venue": "BINANCE_USDM_TESTNET",
        "symbols": [{
            "symbol": "BTCUSDT",
            "assessment": {"direction": "MIXED", "orders_authorized": False},
            "selection": {"decision": "NO_TRADE", "orders_authorized": False},
            "liquidity_evidence": {},
            "in_progress_timeframes": [],
            "timeframe_errors": {},
            "orders_authorized": False,
        }],
        "orders_authorized": False,
    }
    store.publish_intelligence(payload, now=now)
    with store._connect() as conn:
        conn.execute(
            """INSERT INTO paper_ledger.scheduled_job_definitions
               (job_name, interval_seconds, next_run_at, enabled, payload)
               VALUES ('market_intelligence_scan', 900, %s, true, '{}'::jsonb)""",
            (now,),
        )
        conn.execute(
            """INSERT INTO paper_ledger.scheduled_jobs
               (idempotency_key, job_name, scheduled_for, status, attempts, started_at, payload)
               VALUES ('scan-job-123', 'market_intelligence_scan', %s, 'RUNNING', 1, %s, '{}'::jsonb)""",
            (now, now),
        )
    first = store.market_intelligence(now=now)
    assert first["status"] == "HEALTHY"
    assert first["scan_job_status"] == "RUNNING"
    assert first["scan_attempts"] == 1
    assert first["scan_id"] == "scan-123"
    assert first["items"][0]["symbol"] == "BTCUSDT"
    assert first["orders_authorized"] is False

    store.heartbeat("RUNNING", now=now + timedelta(seconds=1), details={
        "analysis_status": "RUNNING", "last_cycle_at": (now + timedelta(seconds=1)).isoformat(),
        "orders_authorized": False,
    })
    after_heartbeat = store.market_intelligence(now=now + timedelta(seconds=1))
    assert after_heartbeat["status"] == "HEALTHY"
    assert after_heartbeat["scan_id"] == "scan-123"
    assert after_heartbeat["orders_authorized"] is False
    store.release(now=now + timedelta(seconds=2))

def test_market_intelligence_publication_rejects_unsafe_and_future_payloads():
    reset_tables()
    now = datetime.now(timezone.utc)
    store = PostgresMarketFeedStore(DSN, worker_id="test-intelligence-safety", owner_id="owner-safety")
    assert store.acquire_lease(now=now) is True
    base = {
        "scan_id": "safe-scan",
        "assessed_at": now.isoformat(),
        "market_type": "FUTURES",
        "environment": "TESTNET",
        "venue": "BINANCE_USDM_TESTNET",
        "symbols": [{
            "symbol": "BTCUSDT",
            "assessment": {"orders_authorized": False},
            "selection": {"orders_authorized": False},
            "liquidity_evidence": {},
            "in_progress_timeframes": [],
            "timeframe_errors": {},
            "orders_authorized": False,
        }],
        "orders_authorized": False,
    }
    unsafe = {**base, "orders_authorized": True}
    with pytest.raises(ValueError, match="deny order authorization"):
        store.publish_intelligence(unsafe, now=now)
    future = {**base, "assessed_at": (now + timedelta(minutes=5)).isoformat()}
    with pytest.raises(ValueError, match="future-dated"):
        store.publish_intelligence(future, now=now)
    store.release(now=now)
