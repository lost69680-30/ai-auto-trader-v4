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
