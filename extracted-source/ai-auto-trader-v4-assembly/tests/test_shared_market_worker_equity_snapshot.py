import threading

import pytest

from backend.shared_market_worker import run_equity_snapshot_scheduler


def test_equity_snapshot_runner_uses_configured_baseline_and_stops_cleanly():
    stop = threading.Event()
    calls = []

    class Ledger:
        def mark_to_market_risk_report(self, **kwargs):
            calls.append(kwargs)
            stop.set()
            return type("Report", (), {
                "marks_fresh": True,
                "account_equity": 10_000.0,
                "open_position_count": 0,
            })()

    run_equity_snapshot_scheduler(
        stop, Ledger(), initial_equity=10_000.0,
        interval_seconds=60, max_mark_age_seconds=30,
        clock=lambda: 123.0,
    )

    assert calls == [{
        "initial_equity": 10_000.0,
        "as_of": 123.0,
        "max_mark_age_seconds": 30.0,
    }]


def test_equity_snapshot_runner_rejects_invalid_baseline_before_writing():
    class Ledger:
        called = False

        def mark_to_market_risk_report(self, **kwargs):
            self.called = True

    ledger = Ledger()
    with pytest.raises(ValueError, match="initial_equity"):
        run_equity_snapshot_scheduler(
            threading.Event(), ledger, initial_equity=0,
            interval_seconds=60, max_mark_age_seconds=30,
            clock=lambda: 123.0,
        )
    assert ledger.called is False
