from datetime import datetime, timedelta, timezone

import pytest

from scripts.solo_heartbeat_monitor import classify_worker_snapshot


UTC = timezone.utc
NOW = datetime(2026, 11, 7, 5, 10, tzinfo=UTC)


def healthy_snapshot():
    return {
        "status": "RUNNING",
        "heartbeat_at": NOW - timedelta(seconds=5),
        "lease_expires_at": NOW + timedelta(seconds=85),
        "orders_authorized": False,
        "details": {
            "scheduled_jobs": {
                "status": "RUNNING",
                "last_poll_at": NOW - timedelta(seconds=10),
            },
        },
        "feed": {
            "total_rows": 10,
            "healthy_rows": 10,
            "stale_rows": 0,
            "unsafe_rows": 0,
            "symbols": [
                "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
                "ADAUSDT", "DOGEUSDT", "TRXUSDT", "LINKUSDT", "AVAXUSDT",
            ],
            "latest_checked_at": NOW - timedelta(seconds=20),
        },
    }


def test_fresh_paper_only_worker_and_scheduler_are_healthy():
    result = classify_worker_snapshot(
        healthy_snapshot(), now=NOW,
        expected_symbols=("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
                          "ADAUSDT", "DOGEUSDT", "TRXUSDT", "LINKUSDT", "AVAXUSDT"),
    )
    assert result["state"] == "HEALTHY"
    assert result["worker_process_healthy"] is True
    assert result["scheduler_healthy"] is True
    assert result["market_data_ready"] is True
    assert result["orders_authorized"] is False


@pytest.mark.parametrize("status", ["STOPPED", "ERROR", "STALE", "MISSING"])
def test_stopped_or_stale_worker_is_not_restarted_or_misreported(status):
    snapshot = healthy_snapshot()
    snapshot["status"] = status
    result = classify_worker_snapshot(snapshot, now=NOW)
    assert result["state"] == "STOPPED"
    assert result["worker_process_healthy"] is False
    assert result["orders_authorized"] is False


def test_old_heartbeat_and_expired_lease_are_stale():
    snapshot = healthy_snapshot()
    snapshot["heartbeat_at"] = NOW - timedelta(seconds=300)
    snapshot["lease_expires_at"] = NOW - timedelta(seconds=1)
    result = classify_worker_snapshot(snapshot, now=NOW)
    assert result["state"] == "STOPPED"
    assert result["worker_process_healthy"] is False
    assert "heartbeat_stale" in result["reasons"]
    assert "lease_expired" in result["reasons"]


def test_stalled_scheduler_is_degraded_without_claiming_worker_death():
    snapshot = healthy_snapshot()
    snapshot["details"]["scheduled_jobs"]["last_poll_at"] = NOW - timedelta(hours=2)
    result = classify_worker_snapshot(snapshot, now=NOW)
    assert result["state"] == "DEGRADED"
    assert result["worker_process_healthy"] is True
    assert result["scheduler_healthy"] is False
    assert result["market_data_ready"] is True


def test_market_feed_degradation_is_reported_separately_from_process_health():
    snapshot = healthy_snapshot()
    snapshot["feed"]["healthy_rows"] = 7
    snapshot["feed"]["stale_rows"] = 3
    result = classify_worker_snapshot(snapshot, now=NOW)
    assert result["state"] == "DEGRADED"
    assert result["worker_process_healthy"] is True
    assert result["market_data_ready"] is False


def test_unsafe_order_authorization_is_a_safety_violation():
    snapshot = healthy_snapshot()
    snapshot["orders_authorized"] = True
    result = classify_worker_snapshot(snapshot, now=NOW)
    assert result["state"] == "SAFETY_VIOLATION"
    assert result["orders_authorized"] is True
    assert result["worker_process_healthy"] is False


@pytest.mark.parametrize(
    "timestamp",
    ["not-a-time", datetime(2026, 11, 7, 5, 9), None],
)
def test_invalid_or_naive_heartbeat_timestamp_fails_closed(timestamp):
    snapshot = healthy_snapshot()
    snapshot["heartbeat_at"] = timestamp
    result = classify_worker_snapshot(snapshot, now=NOW)
    assert result["state"] == "UNKNOWN"
    assert result["worker_process_healthy"] is False


def test_missing_database_snapshot_is_unknown():
    result = classify_worker_snapshot(None, now=NOW)
    assert result["state"] == "UNKNOWN"
    assert result["orders_authorized"] is False


def test_missing_expected_symbol_is_market_data_degradation():
    snapshot = healthy_snapshot()
    snapshot["feed"]["symbols"].remove("AVAXUSDT")
    snapshot["feed"]["total_rows"] = 9
    snapshot["feed"]["healthy_rows"] = 9
    result = classify_worker_snapshot(
        snapshot, now=NOW,
        expected_symbols=("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
                          "ADAUSDT", "DOGEUSDT", "TRXUSDT", "LINKUSDT", "AVAXUSDT"),
    )
    assert result["state"] == "DEGRADED"
    assert result["market_data_ready"] is False
    assert "symbol_coverage_mismatch" in result["reasons"]


def test_unsafe_feed_snapshot_is_safety_violation():
    snapshot = healthy_snapshot()
    snapshot["feed"]["unsafe_rows"] = 1
    result = classify_worker_snapshot(snapshot, now=NOW)
    assert result["state"] == "SAFETY_VIOLATION"
    assert result["market_data_ready"] is False


def test_degraded_worker_status_can_still_be_process_healthy():
    snapshot = healthy_snapshot()
    snapshot["status"] = "DEGRADED"
    snapshot["feed"]["healthy_rows"] = 7
    snapshot["feed"]["stale_rows"] = 3
    result = classify_worker_snapshot(snapshot, now=NOW)
    assert result["state"] == "DEGRADED"
    assert result["worker_process_healthy"] is True
    assert result["market_data_ready"] is False


def test_degraded_heartbeat_status_never_reports_fully_healthy():
    snapshot = healthy_snapshot()
    snapshot["status"] = "DEGRADED"
    result = classify_worker_snapshot(snapshot, now=NOW)
    assert result["state"] == "DEGRADED"
    assert result["worker_process_healthy"] is True
    assert result["scheduler_healthy"] is True
    assert result["market_data_ready"] is True


def test_only_confirmed_stopped_worker_requires_one_wake_request():
    from scripts.solo_heartbeat_monitor import worker_wake_required

    stopped = healthy_snapshot()
    stopped["status"] = "STOPPED"
    assert worker_wake_required(
        classify_worker_snapshot(stopped, now=NOW)
    ) is True

    degraded_feed = healthy_snapshot()
    degraded_feed["feed"]["healthy_rows"] = 7
    degraded_feed["feed"]["stale_rows"] = 3
    assert worker_wake_required(
        classify_worker_snapshot(degraded_feed, now=NOW)
    ) is False

    stalled_scheduler = healthy_snapshot()
    stalled_scheduler["details"]["scheduled_jobs"]["last_poll_at"] = NOW - timedelta(hours=2)
    assert worker_wake_required(
        classify_worker_snapshot(stalled_scheduler, now=NOW)
    ) is False


def test_unknown_or_unsafe_worker_state_never_triggers_wake():
    from scripts.solo_heartbeat_monitor import worker_wake_required

    assert worker_wake_required(
        classify_worker_snapshot(None, now=NOW)
    ) is False

    unsafe = healthy_snapshot()
    unsafe["orders_authorized"] = True
    assert worker_wake_required(
        classify_worker_snapshot(unsafe, now=NOW)
    ) is False


def test_confirmed_stopped_worker_sends_one_validated_liveness_request():
    from scripts.solo_heartbeat_monitor import request_worker_wake

    stopped = healthy_snapshot()
    stopped["status"] = "STOPPED"
    classification = classify_worker_snapshot(stopped, now=NOW)
    calls = []

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"status":"ok","mode":"PAPER_ONLY","orders_authorized":false}'

    def fake_open(request, timeout):
        calls.append((request.full_url, timeout))
        return FakeResponse()

    result = request_worker_wake(classification, opener=fake_open)

    assert result["attempted"] is True
    assert result["response_validated"] is True
    assert result["error_type"] is None
    assert len(calls) == 1
    assert calls[0][0] == "https://solo-futures-websocket-staging.onrender.com/healthz"


def test_feed_degradation_does_not_send_a_liveness_request():
    from scripts.solo_heartbeat_monitor import request_worker_wake

    degraded = healthy_snapshot()
    degraded["feed"]["healthy_rows"] = 7
    degraded["feed"]["stale_rows"] = 3
    classification = classify_worker_snapshot(degraded, now=NOW)
    calls = []

    result = request_worker_wake(
        classification,
        opener=lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert result["attempted"] is False
    assert result["response_validated"] is False
    assert calls == []


def test_wake_response_must_prove_liveness_and_order_authorization_denial():
    from scripts.solo_heartbeat_monitor import request_worker_wake

    stopped = healthy_snapshot()
    stopped["status"] = "STOPPED"
    classification = classify_worker_snapshot(stopped, now=NOW)

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"status":"ok","mode":"PAPER_ONLY","orders_authorized":true}'

    result = request_worker_wake(
        classification,
        opener=lambda request, timeout: FakeResponse(),
    )

    assert result["attempted"] is True
    assert result["response_validated"] is False


def test_recovery_wait_confirms_worker_and_scheduler_from_read_only_snapshots(monkeypatch):
    from scripts import solo_heartbeat_monitor as monitor

    snapshots = [healthy_snapshot()]
    monkeypatch.setattr(monitor, "load_worker_snapshot", lambda *args: snapshots[-1])

    result = monitor.wait_for_worker_recovery(
        "postgresql://read-only",
        "solo-market-worker",
        max_attempts=1,
        poll_interval_seconds=0,
        sleeper=lambda _: None,
        now_provider=lambda: NOW,
    )

    assert result["state"] == "HEALTHY"
    assert result["worker_process_healthy"] is True
    assert result["scheduler_healthy"] is True
    assert result["orders_authorized"] is False
