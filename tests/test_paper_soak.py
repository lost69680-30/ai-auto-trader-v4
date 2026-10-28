from app.engines.paper_soak import build_deterministic_soak_events, run_paper_soak


def test_deterministic_soak_reaches_fills_exits_and_restart_recovery(tmp_path):
    report = run_paper_soak(database_path=tmp_path / "soak.sqlite3", hours=1)

    assert report.simulated_hours == 1
    assert report.events_processed >= 12 * 5
    assert report.orders_recorded == 12
    assert report.fills == 12
    assert report.closed_positions == 12
    assert report.open_positions == 0
    assert report.protection_exits == 12
    assert report.restart_events == 1
    assert report.orders_authorized is False
    assert report.emergency_stop_active is False


def test_soak_event_factory_contains_stale_quotes_and_outcome_mix():
    events = build_deterministic_soak_events(hours=3)
    types = [type(event).__name__ for event in events]
    assert "RestartEvent" in types
    assert "MarketUpdateEvent" in types
    assert len([event for event in events if type(event).__name__ == "RecordIntentEvent"]) == 36
    assert len([event for event in events if type(event).__name__ == "FillEvent"]) == 36


def test_24_hour_synthetic_soak_metrics_are_consistent(tmp_path):
    report = run_paper_soak(database_path=tmp_path / "soak-24h.sqlite3", hours=24)

    assert report.passed is True
    assert report.orders_recorded == 288
    assert report.fills == 288
    assert report.closed_positions == 288
    assert report.open_positions == 0
    assert report.restart_events == 24
    assert report.stale_market_updates_rejected == 9
    assert report.orders_authorized is False
