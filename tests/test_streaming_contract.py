from datetime import datetime, timedelta, timezone

import pytest

from backend.streaming_contract import (
    authenticate_stream_token,
    make_market_snapshot_event,
)


TOKEN = "test-stream-token-with-at-least-thirty-two-characters"


def test_market_snapshot_event_has_versioned_read_only_contract():
    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    event = make_market_snapshot_event(
        {
            "symbol": "BTCUSDT",
            "mark_price": 65000.0,
            "mark_observed_at": (now - timedelta(seconds=2)).isoformat(),
            "status": "HEALTHY",
            "orders_authorized": False,
        },
        now=now,
        max_age_seconds=90,
    )

    assert event == {
        "schema_version": 1,
        "type": "market.snapshot",
        "symbol": "BTCUSDT",
        "market_type": "FUTURES",
        "mark_price": 65000.0,
        "observed_at": (now - timedelta(seconds=2)).isoformat(),
        "status": "HEALTHY",
        "orders_authorized": False,
    }


@pytest.mark.parametrize(
    "snapshot",
    [
        {"symbol": "btcusdt", "mark_price": 10, "mark_observed_at": "2026-10-29T12:00:00+00:00", "status": "HEALTHY", "orders_authorized": False},
        {"symbol": "BTCUSDT", "mark_price": float("nan"), "mark_observed_at": "2026-10-29T12:00:00+00:00", "status": "HEALTHY", "orders_authorized": False},
        {"symbol": "BTCUSDT", "mark_price": 10, "mark_observed_at": "2026-10-29T12:00:00+00:00", "status": "HEALTHY", "orders_authorized": True},
        {"symbol": "BTCUSDT", "mark_price": 10, "mark_observed_at": "2026-10-29T12:00:00+00:00", "status": "OFFLINE", "orders_authorized": False},
    ],
)
def test_market_event_rejects_invalid_or_unsafe_snapshots(snapshot):
    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        make_market_snapshot_event(snapshot, now=now, max_age_seconds=90)


def test_market_event_rejects_stale_or_future_data():
    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    for observed in (now - timedelta(seconds=91), now + timedelta(seconds=2)):
        snapshot = {
            "symbol": "BTCUSDT",
            "mark_price": 65000.0,
            "mark_observed_at": observed.isoformat(),
            "status": "HEALTHY",
            "orders_authorized": False,
        }
        with pytest.raises(ValueError):
            make_market_snapshot_event(snapshot, now=now, max_age_seconds=90)


def test_stream_token_authentication_is_constant_time_safe_and_fail_closed():
    assert authenticate_stream_token(TOKEN, TOKEN)
    assert not authenticate_stream_token(TOKEN, "wrong")
    assert not authenticate_stream_token("", TOKEN)
    assert not authenticate_stream_token("short", TOKEN)


def test_spot_market_snapshot_uses_last_price_and_never_futures_fields():
    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    event = make_market_snapshot_event(
        {
            "symbol": "BTCUSDT",
            "market_type": "SPOT",
            "last_price": 65000.0,
            "last_observed_at": (now - timedelta(seconds=2)).isoformat(),
            "status": "HEALTHY",
            "orders_authorized": False,
        },
        now=now,
        max_age_seconds=90,
    )
    assert event["market_type"] == "SPOT"
    assert event["last_price"] == 65000.0
    assert "mark_price" not in event
    assert event["orders_authorized"] is False


def test_spot_snapshot_rejects_futures_only_price_fields():
    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        make_market_snapshot_event(
            {
                "symbol": "BTCUSDT",
                "market_type": "SPOT",
                "mark_price": 65000.0,
                "mark_observed_at": now.isoformat(),
                "status": "HEALTHY",
                "orders_authorized": False,
            },
            now=now,
        )

def test_futures_market_snapshot_preserves_index_and_funding_context():
    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    next_funding = now + timedelta(hours=8)
    event = make_market_snapshot_event(
        {
            "symbol": "BTCUSDT",
            "market_type": "FUTURES",
            "mark_price": 65000.0,
            "mark_observed_at": (now - timedelta(seconds=2)).isoformat(),
            "index_price": 64990.0,
            "funding_rate": 0.0001,
            "next_funding_at": next_funding.isoformat(),
            "status": "HEALTHY",
            "orders_authorized": False,
        },
        now=now,
        max_age_seconds=90,
    )

    assert event["market_type"] == "FUTURES"
    assert event["mark_price"] == 65000.0
    assert event["index_price"] == 64990.0
    assert event["funding_rate"] == 0.0001
    assert event["next_funding_at"] == next_funding.isoformat()
    assert event["orders_authorized"] is False


def test_futures_market_snapshot_rejects_partial_or_expired_funding_context():
    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    base = {
        "symbol": "BTCUSDT",
        "market_type": "FUTURES",
        "mark_price": 65000.0,
        "mark_observed_at": now.isoformat(),
        "index_price": 64990.0,
        "funding_rate": 0.0001,
        "next_funding_at": (now + timedelta(hours=8)).isoformat(),
        "status": "HEALTHY",
        "orders_authorized": False,
    }
    partial = dict(base)
    partial["funding_rate"] = None
    expired = dict(base)
    expired["next_funding_at"] = (now - timedelta(seconds=1)).isoformat()

    with pytest.raises(ValueError):
        make_market_snapshot_event(partial, now=now)
    with pytest.raises(ValueError):
        make_market_snapshot_event(expired, now=now)

def test_futures_market_snapshot_preserves_index_and_funding_context():
    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    next_funding = now + timedelta(hours=8)
    event = make_market_snapshot_event(
        {
            "symbol": "BTCUSDT",
            "market_type": "FUTURES",
            "mark_price": 65000.0,
            "mark_observed_at": (now - timedelta(seconds=2)).isoformat(),
            "index_price": 64990.0,
            "funding_rate": 0.0001,
            "next_funding_at": next_funding.isoformat(),
            "status": "HEALTHY",
            "orders_authorized": False,
        },
        now=now,
        max_age_seconds=90,
    )

    assert event["market_type"] == "FUTURES"
    assert event["mark_price"] == 65000.0
    assert event["index_price"] == 64990.0
    assert event["funding_rate"] == 0.0001
    assert event["next_funding_at"] == next_funding.isoformat()
    assert event["orders_authorized"] is False


def test_futures_market_snapshot_rejects_partial_or_expired_funding_context():
    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    base = {
        "symbol": "BTCUSDT",
        "market_type": "FUTURES",
        "mark_price": 65000.0,
        "mark_observed_at": now.isoformat(),
        "index_price": 64990.0,
        "funding_rate": 0.0001,
        "next_funding_at": (now + timedelta(hours=8)).isoformat(),
        "status": "HEALTHY",
        "orders_authorized": False,
    }
    partial = dict(base)
    partial["funding_rate"] = None
    expired = dict(base)
    expired["next_funding_at"] = (now - timedelta(seconds=1)).isoformat()

    with pytest.raises(ValueError):
        make_market_snapshot_event(partial, now=now)
    with pytest.raises(ValueError):
        make_market_snapshot_event(expired, now=now)
