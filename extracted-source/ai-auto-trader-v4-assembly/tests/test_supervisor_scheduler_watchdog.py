from datetime import datetime, timedelta, timezone

from backend.shared_market_supervisor import worker_heartbeat_requires_restart
from backend.shared_market_store import PostgresMarketFeedStore


def test_supervisor_restarts_when_scheduler_progress_stalls_but_worker_heartbeat_is_fresh():
    now = datetime.now(timezone.utc)
    status = {
        "status": "HEALTHY",
        "worker_status": "RUNNING",
        "age_seconds": 5,
        "details": {
            "scheduled_jobs": {
                "status": "RUNNING",
                "last_poll_started_at": (now - timedelta(seconds=901)).isoformat(),
                "last_poll_at": (now - timedelta(seconds=901)).isoformat(),
            }
        },
    }

    assert worker_heartbeat_requires_restart(
        status, uptime_seconds=600, startup_grace_seconds=30,
        scheduler_stale_seconds=900, now=now,
    ) is True


def test_supervisor_does_not_restart_when_scheduler_has_recent_progress_even_if_job_failed():
    now = datetime.now(timezone.utc)
    status = {
        "status": "DEGRADED",
        "worker_status": "DEGRADED",
        "age_seconds": 5,
        "details": {
            "scheduled_jobs": {
                "status": "DEGRADED",
                "last_poll_started_at": (now - timedelta(seconds=10)).isoformat(),
                "last_poll_at": (now - timedelta(seconds=5)).isoformat(),
                "last_error": "TemporaryResearchSourceError",
                "last_dispatch_status": "FAILED",
            }
        },
    }

    assert worker_heartbeat_requires_restart(
        status, uptime_seconds=600, startup_grace_seconds=30,
        scheduler_stale_seconds=900, now=now,
    ) is False


def test_supervisor_does_not_infer_scheduler_failure_from_missing_or_invalid_progress_timestamp():
    for details in ({}, {"scheduled_jobs": {"last_poll_at": "not-a-timestamp"}}):
        status = {
            "status": "HEALTHY", "worker_status": "RUNNING", "age_seconds": 5,
            "details": details,
        }
        assert worker_heartbeat_requires_restart(
            status, uptime_seconds=600, startup_grace_seconds=30,
            scheduler_stale_seconds=900,
        ) is False


def test_shared_market_status_exposes_diagnostic_details_to_supervisor_without_credentials(monkeypatch):
    now = datetime.now(timezone.utc)
    diagnostic = {
        "process_id": 1234,
        "analysis_status": "ANALYSIS_FAILED: api_token=do-not-expose",
        "analysis_error": "PrivateFailure: api_token=do-not-expose",
        "scheduled_jobs": {
            "last_poll_at": now.isoformat(),
            "last_error": "PrivateFailure: api_token=do-not-expose",
        },
        "orders_authorized": False,
    }
    expected_diagnostic = {
        "process_id": 1234,
        "analysis_status": "ANALYSIS_FAILED",
        "analysis_error_type": "PrivateFailure",
        "scheduled_jobs": {
            "last_poll_at": now.isoformat(),
            "last_error_type": "PrivateFailure",
        },
        "orders_authorized": False,
    }
    store = object.__new__(PostgresMarketFeedStore)
    store.worker_id = "solo-market-worker"
    store.max_age_seconds = 90
    monkeypatch.setattr(store, "_read", lambda: ({
        "worker_id": store.worker_id,
        "owner_id": "opaque-owner",
        "heartbeat_at": now,
        "lease_expires_at": now + timedelta(seconds=90),
        "status": "RUNNING",
        "details": diagnostic,
    }, [{
        "status": "HEALTHY",
        "checked_at": now.isoformat(),
        "orders_authorized": False,
    }]))

    result = store.status()

    assert result["details"] == expected_diagnostic
    assert "do-not-expose" not in str(result)
    assert result["orders_authorized"] is False
    assert "dsn" not in result
    assert "password" not in str(result).lower()


def test_scheduled_job_runner_publishes_progress_before_dispatch_and_after_completion():
    import threading

    from backend.shared_market_worker import run_scheduled_jobs

    stop = threading.Event()
    health = {}

    class Scheduler:
        def enqueue_due(self):
            return 1

    class Dispatcher:
        last_run_status = "FAILED"
        last_error = "TemporarySourceFailure"

        def run_once(self):
            assert health.get("last_poll_started_at")
            stop.set()
            return False

    run_scheduled_jobs(
        stop, Scheduler(), Dispatcher(), poll_interval=1,
        health=health,
    )

    assert health["status"] == "DEGRADED"
    assert health["last_poll_started_at"]
    assert health["last_poll_at"]
    assert health["last_error"] == "TemporarySourceFailure"


def test_scheduler_watchdog_threshold_is_bounded_and_defaults_to_fifteen_minutes():
    import pytest

    from backend.shared_market_supervisor import configured_scheduler_stale_seconds

    assert configured_scheduler_stale_seconds({}) == 900
    assert configured_scheduler_stale_seconds({
        "PAPER_SUPERVISOR_SCHEDULER_STALE_SECONDS": "600"
    }) == 600
    for invalid in ("59", "3601", "not-a-number"):
        with pytest.raises(ValueError, match="PAPER_SUPERVISOR_SCHEDULER_STALE_SECONDS"):
            configured_scheduler_stale_seconds({
                "PAPER_SUPERVISOR_SCHEDULER_STALE_SECONDS": invalid
            })


def test_supervisor_does_not_restart_a_running_worker_for_stale_market_snapshots():
    now = datetime.now(timezone.utc)
    status = {
        "status": "STALE",
        "worker_status": "RUNNING",
        "lease_expired": False,
        "age_seconds": 5,
        "details": {
            "scheduled_jobs": {
                "last_poll_started_at": (now - timedelta(seconds=10)).isoformat(),
                "last_poll_at": (now - timedelta(seconds=5)).isoformat(),
            }
        },
    }

    assert worker_heartbeat_requires_restart(
        status, uptime_seconds=600, startup_grace_seconds=30,
        scheduler_stale_seconds=900, now=now,
    ) is False


def test_strategy_research_history_keeps_four_validation_fold_minimum(monkeypatch):
    import pytest

    from backend.shared_market_worker import configured_strategy_research_candle_limit

    monkeypatch.delenv("PAPER_STRATEGY_RESEARCH_CANDLES", raising=False)
    assert configured_strategy_research_candle_limit() == 4500

    monkeypatch.setenv("PAPER_STRATEGY_RESEARCH_CANDLES", "3000")
    with pytest.raises(ValueError, match="PAPER_STRATEGY_RESEARCH_CANDLES"):
        configured_strategy_research_candle_limit()


def test_degraded_worker_heartbeat_preserves_scheduler_progress_details():
    import ast
    from pathlib import Path

    source = (
        Path(__file__).parents[1] / "backend/shared_market_worker.py"
    ).read_text(encoding="utf-8")
    module = ast.parse(source)

    for handler in ast.walk(module):
        if not isinstance(handler, ast.ExceptHandler):
            continue
        for call in ast.walk(handler):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "heartbeat"
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and call.args[0].value == "DEGRADED"
            ):
                continue
            details = next(
                (keyword.value for keyword in call.keywords if keyword.arg == "details"),
                call.args[1] if len(call.args) > 1 else None,
            )
            if not isinstance(details, ast.Dict):
                continue
            keys = {
                key.value
                for key in details.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            assert "scheduled_jobs" in keys
            return

    raise AssertionError("degraded worker heartbeat must include scheduled_jobs diagnostics")
