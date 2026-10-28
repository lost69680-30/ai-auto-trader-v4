from dataclasses import replace

from app.engines.orchestrator import PaperOrderIntent
from app.engines.paper_replay import (
    EmergencyStopEvent,
    FillEvent,
    MarketUpdateEvent,
    PaperReplayHarness,
    RecordIntentEvent,
    RestartEvent,
)


def intent(proposal_id="p1", **changes):
    base = PaperOrderIntent(
        proposal_id=proposal_id,
        signal_id=f"s-{proposal_id}",
        symbol="BTCUSDT",
        side="LONG",
        quantity=0.01,
        entry_price=50_000,
        stop_loss=49_000,
        take_profit=52_000,
        recorded_at=100,
    )
    return replace(base, **changes)


def test_replay_rejects_stale_and_future_market_updates(tmp_path):
    report = PaperReplayHarness(tmp_path / "replay.db", max_market_age_seconds=5).run([
        MarketUpdateEvent(at=110, observed_at=100, prices={"BTCUSDT": 50_000}),
        MarketUpdateEvent(at=111, observed_at=112, prices={"BTCUSDT": 50_100}),
        MarketUpdateEvent(at=112, observed_at=112, prices={"BTCUSDT": 50_200}),
        MarketUpdateEvent(at=113, observed_at=111, prices={"BTCUSDT": 50_300}),
    ])

    assert [step.status for step in report.steps] == ["STALE_MARKET_REJECTED", "FUTURE_MARKET_REJECTED", "MARKET_UPDATED", "OUT_OF_ORDER_MARKET_REJECTED"]
    assert report.orders_authorized is False


def test_replay_duplicate_intents_and_fills_remain_idempotent_across_restart(tmp_path):
    report = PaperReplayHarness(tmp_path / "replay.db").run([
        RecordIntentEvent(at=100, intent=intent()),
        RecordIntentEvent(at=101, intent=intent()),
        FillEvent(at=102, proposal_id="p1", market_price=50_000, fee_rate=0, slippage_bps=0),
        RestartEvent(at=102),
        FillEvent(at=102, proposal_id="p1", market_price=50_000, fee_rate=0, slippage_bps=0),
    ])

    assert [step.status for step in report.steps] == ["RECORDED", "DUPLICATE", "FILLED", "RESTARTED", "ALREADY_FILLED"]
    assert len(report.orders) == 1
    assert len(report.fills) == 1
    assert len(report.open_positions) == 1


def test_replay_protection_exit_survives_restart_and_emergency_stop_blocks_entries(tmp_path):
    report = PaperReplayHarness(tmp_path / "replay.db").run([
        RecordIntentEvent(at=100, intent=intent()),
        FillEvent(at=101, proposal_id="p1", market_price=50_000, fee_rate=0, slippage_bps=0),
        RestartEvent(at=102),
        MarketUpdateEvent(at=103, observed_at=103, prices={"BTCUSDT": 48_900}),
        EmergencyStopEvent(at=104, reason="replay safety test", prices={"BTCUSDT": 48_800}),
        RestartEvent(at=105),
        RecordIntentEvent(at=106, intent=intent("p2", recorded_at=106)),
    ])

    assert report.steps[3].status == "PROTECTION_EXIT"
    assert report.steps[4].status == "EMERGENCY_STOP_ACTIVE"
    assert report.steps[5].status == "RESTARTED"
    assert report.steps[6].status == "BLOCKED_BY_EMERGENCY_STOP"
    assert report.emergency_stop_active is True
    assert report.open_positions == ()
    assert report.orders_authorized is False


def test_replay_rejects_event_time_regression_without_mutating_ledger(tmp_path):
    report = PaperReplayHarness(tmp_path / "replay.db").run([
        RecordIntentEvent(at=100, intent=intent()),
        RecordIntentEvent(at=99, intent=intent("p2", recorded_at=99)),
    ])

    assert [step.status for step in report.steps] == ["RECORDED", "OUT_OF_ORDER_EVENT_REJECTED"]
    assert [order.proposal_id for order in report.orders] == ["p1"]


def test_stale_price_that_crosses_stop_does_not_close_open_position(tmp_path):
    report = PaperReplayHarness(tmp_path / "replay.db", max_market_age_seconds=2).run([
        RecordIntentEvent(at=100, intent=intent()),
        FillEvent(at=101, proposal_id="p1", market_price=50_000, fee_rate=0, slippage_bps=0),
        MarketUpdateEvent(at=110, observed_at=101, prices={"BTCUSDT": 48_000}),
    ])

    assert report.steps[-1].status == "STALE_MARKET_REJECTED"
    assert len(report.open_positions) == 1
    assert report.open_positions[0].mark_price == 50_000
