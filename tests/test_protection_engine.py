from dataclasses import replace

import pytest

from app.engines.orchestrator import PaperOrderIntent
from app.engines.paper_ledger import SQLitePaperLedger
from app.engines.protection import PaperProtectionEngine


def make_intent(proposal_id="p1", symbol="BTCUSDT", side="LONG", **changes):
    values = dict(
        proposal_id=proposal_id,
        signal_id=f"s-{proposal_id}",
        symbol=symbol,
        side=side,
        quantity=0.01,
        entry_price=50_000,
        stop_loss=49_000 if side == "LONG" else 51_000,
        take_profit=52_000 if side == "LONG" else 48_000,
        recorded_at=1000,
    )
    values.update(changes)
    return PaperOrderIntent(**values)


def open_position(ledger, intent=None):
    intent = intent or make_intent()
    assert ledger.record_intent(intent) == "RECORDED"
    result = ledger.simulate_fill(
        intent.proposal_id,
        market_price=intent.entry_price,
        filled_at=1001,
        fee_rate=0,
        slippage_bps=0,
    )
    assert result.status == "FILLED"
    return result.position


def test_emergency_stop_persists_cancels_pending_orders_and_blocks_new_intents(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    ledger.record_intent(make_intent())
    protection = PaperProtectionEngine(ledger, fee_rate=0, slippage_bps=0)

    result = protection.activate_emergency_stop(observed_at=1002, reason="operator requested stop")

    assert result.emergency_stop_active is True
    assert result.cancelled_order_ids == ("p1",)
    assert ledger.orders[0].status == "CANCELLED"
    assert ledger.record_intent(make_intent(proposal_id="p2")) == "BLOCKED_BY_EMERGENCY_STOP"
    assert ledger.simulate_fill("p1", market_price=50_000, filled_at=1003).status == "EMERGENCY_STOP_ACTIVE"
    assert ledger.orders_authorized is False
    ledger.close()


def test_emergency_stop_state_survives_restart_and_cancelled_intent_stays_cancelled(tmp_path):
    path = tmp_path / "paper.db"
    ledger = SQLitePaperLedger(path)
    ledger.record_intent(make_intent())
    PaperProtectionEngine(ledger).activate_emergency_stop(observed_at=1002, reason="incident")
    ledger.close()

    recovered = SQLitePaperLedger(path)
    assert recovered.emergency_stop_active is True
    assert recovered.emergency_stop_reason == "incident"
    assert recovered.orders[0].status == "CANCELLED"
    with pytest.raises(ValueError, match="reason"):
        PaperProtectionEngine(recovered).reset_emergency_stop(observed_at=1003, reason=" ")
    assert PaperProtectionEngine(recovered).reset_emergency_stop(observed_at=1003, reason="operator reviewed") is True
    assert recovered.emergency_stop_active is False
    assert recovered.simulate_fill("p1", market_price=50_000, filled_at=1004).status == "ORDER_CANCELLED"
    recovered.close()


def test_emergency_stop_activation_is_idempotent_and_cannot_clear_with_open_positions(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    open_position(ledger)
    engine = PaperProtectionEngine(ledger, fee_rate=0, slippage_bps=0)
    first = engine.activate_emergency_stop(observed_at=1002, reason="operator stop")
    second = engine.activate_emergency_stop(observed_at=1003, reason="repeat signal")

    assert first.emergency_stop_active is True
    assert second.cancelled_order_ids == ()
    with pytest.raises(ValueError, match="positions remain open"):
        ledger.clear_emergency_stop(occurred_at=1004, reason="direct unsafe reset")
    assert ledger.emergency_stop_active is True
    ledger.close()


def test_long_stop_loss_triggers_automatic_paper_exit(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    open_position(ledger)
    engine = PaperProtectionEngine(ledger, fee_rate=0, slippage_bps=0)

    result = engine.process_market_update({"BTCUSDT": 48_900}, observed_at=1002)

    assert len(result.closed_positions) == 1
    position = result.closed_positions[0]
    assert position.status == "CLOSED"
    assert position.close_price == pytest.approx(48_900)
    assert result.close_reasons["p1"] == "STOP_LOSS"
    assert ledger.open_positions == ()
    ledger.close()


def test_short_take_profit_triggers_automatic_paper_exit(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    open_position(ledger, make_intent(side="SHORT"))
    engine = PaperProtectionEngine(ledger, fee_rate=0, slippage_bps=0)

    result = engine.process_market_update({"BTCUSDT": 47_900}, observed_at=1002)

    assert result.close_reasons["p1"] == "TAKE_PROFIT"
    assert result.closed_positions[0].close_price == pytest.approx(47_900)
    assert ledger.open_positions == ()
    ledger.close()


def test_long_take_profit_triggers_automatic_paper_exit(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    open_position(ledger)
    engine = PaperProtectionEngine(ledger, fee_rate=0, slippage_bps=0)

    result = engine.process_market_update({"BTCUSDT": 52_100}, observed_at=1002)

    assert result.close_reasons["p1"] == "TAKE_PROFIT"
    assert result.closed_positions[0].close_price == pytest.approx(52_100)
    close_event = next(event for event in ledger.audit_events if event.event_type == "PAPER_POSITION_CLOSED")
    assert close_event.payload["close_reason"] == "TAKE_PROFIT"
    ledger.close()


def test_short_stop_loss_triggers_automatic_paper_exit(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    open_position(ledger, make_intent(side="SHORT"))
    engine = PaperProtectionEngine(ledger, fee_rate=0, slippage_bps=0)

    result = engine.process_market_update({"BTCUSDT": 51_100}, observed_at=1002)

    assert result.close_reasons["p1"] == "STOP_LOSS"
    assert result.closed_positions[0].close_price == pytest.approx(51_100)
    ledger.close()


def test_trailing_stop_ratchets_up_and_closes_on_retracement(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    open_position(ledger)
    engine = PaperProtectionEngine(
        ledger, trailing_distance_pct=0.02, trailing_activation_pct=0.01,
        fee_rate=0, slippage_bps=0,
    )

    favorable = engine.process_market_update({"BTCUSDT": 51_000}, observed_at=1002)
    assert favorable.closed_positions == ()
    assert ledger.open_positions[0].stop_loss == pytest.approx(49_980)

    retracement = engine.process_market_update({"BTCUSDT": 49_900}, observed_at=1003)
    assert retracement.close_reasons["p1"] == "STOP_LOSS"
    assert retracement.closed_positions[0].close_price == pytest.approx(49_900)
    assert ledger.open_positions == ()
    assert any(event.event_type == "PAPER_TRAILING_STOP_UPDATED" for event in ledger.audit_events)
    ledger.close()


def test_trailing_high_water_and_stop_survive_database_restart(tmp_path):
    path = tmp_path / "paper.db"
    ledger = SQLitePaperLedger(path)
    open_position(ledger)
    engine = PaperProtectionEngine(
        ledger, trailing_distance_pct=0.02, trailing_activation_pct=0.01,
        fee_rate=0, slippage_bps=0,
    )
    engine.process_market_update({"BTCUSDT": 51_000}, observed_at=1002)
    ledger.close()

    recovered = SQLitePaperLedger(path)
    engine2 = PaperProtectionEngine(
        recovered, trailing_distance_pct=0.02, trailing_activation_pct=0.01,
        fee_rate=0, slippage_bps=0,
    )
    engine2.process_market_update({"BTCUSDT": 50_800}, observed_at=1003)
    assert recovered.open_positions[0].stop_loss == pytest.approx(49_980)
    recovered.close()


def test_emergency_stop_with_prices_paper_flattens_positions(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    open_position(ledger)
    engine = PaperProtectionEngine(ledger, fee_rate=0, slippage_bps=0)

    result = engine.activate_emergency_stop(
        observed_at=1002, reason="operator requested flatten",
        market_prices={"BTCUSDT": 49_700},
    )

    assert result.emergency_stop_active is True
    assert result.close_reasons["p1"] == "EMERGENCY_STOP"
    assert result.closed_positions[0].close_price == pytest.approx(49_700)
    assert ledger.open_positions == ()
    assert ledger.orders_authorized is False
    ledger.close()


def test_emergency_stop_without_prices_halts_entries_until_a_price_arrives(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    open_position(ledger)
    engine = PaperProtectionEngine(ledger, fee_rate=0, slippage_bps=0)

    activated = engine.activate_emergency_stop(observed_at=1002, reason="feed unavailable")
    assert activated.emergency_stop_active is True
    assert len(ledger.open_positions) == 1  # no fabricated close price
    with pytest.raises(ValueError, match="positions remain open"):
        engine.reset_emergency_stop(observed_at=1002, reason="unsafe reset")

    resumed = engine.process_market_update({"BTCUSDT": 49_800}, observed_at=1003)
    assert resumed.close_reasons["p1"] == "EMERGENCY_STOP"
    assert ledger.open_positions == ()
    assert engine.reset_emergency_stop(observed_at=1004, reason="positions verified flat") is True
    ledger.close()


def test_protection_checks_previous_persisted_mark_before_accepting_new_mark(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    open_position(ledger)
    ledger.mark_positions({"BTCUSDT": 48_900}, observed_at=1002)
    engine = PaperProtectionEngine(ledger, fee_rate=0, slippage_bps=0)

    # Recovery path: the prior process persisted a stop-crossing mark but died before closing.
    result = engine.process_market_update({"BTCUSDT": 50_100}, observed_at=1003)

    assert result.close_reasons["p1"] == "STOP_LOSS"
    assert result.closed_positions[0].close_price == pytest.approx(48_900)
    assert ledger.open_positions == ()
    ledger.close()


def test_protection_rejects_invalid_trailing_configuration(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    with pytest.raises(ValueError):
        PaperProtectionEngine(ledger, trailing_distance_pct=0)
    with pytest.raises(ValueError):
        PaperProtectionEngine(ledger, trailing_distance_pct=0.02, trailing_activation_pct=-0.1)
    ledger.close()


def test_short_trailing_stop_ratchets_down_then_exits_on_rebound(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    open_position(ledger, make_intent(side="SHORT"))
    engine = PaperProtectionEngine(
        ledger, trailing_distance_pct=0.02, trailing_activation_pct=0.01,
        fee_rate=0, slippage_bps=0,
    )

    favorable = engine.process_market_update({"BTCUSDT": 49_000}, observed_at=1002)
    assert favorable.closed_positions == ()
    assert ledger.open_positions[0].stop_loss == pytest.approx(49_980)

    rebound = engine.process_market_update({"BTCUSDT": 50_100}, observed_at=1003)
    assert rebound.close_reasons["p1"] == "STOP_LOSS"
    assert ledger.open_positions == ()
    ledger.close()


def test_trailing_stop_recovers_favorable_mark_persisted_before_process_crash(tmp_path):
    path = tmp_path / "paper.db"
    ledger = SQLitePaperLedger(path)
    open_position(ledger)
    # Simulate a crash immediately after mark persistence but before the trailing update.
    ledger.mark_positions({"BTCUSDT": 51_000}, observed_at=1002)
    ledger.close()

    recovered = SQLitePaperLedger(path)
    engine = PaperProtectionEngine(
        recovered, trailing_distance_pct=0.02, trailing_activation_pct=0.01,
        fee_rate=0, slippage_bps=0,
    )
    engine.process_market_update({"BTCUSDT": 50_800}, observed_at=1003)
    assert recovered.open_positions[0].stop_loss == pytest.approx(49_980)
    recovered.close()


def test_conflicting_case_normalized_market_prices_fail_closed(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    open_position(ledger)
    engine = PaperProtectionEngine(ledger, fee_rate=0, slippage_bps=0)

    with pytest.raises(ValueError, match="Conflicting market prices"):
        engine.process_market_update({"BTCUSDT": 50_000, "btcusdt": 49_000}, observed_at=1002)
    assert ledger.open_positions[0].mark_price == pytest.approx(50_000)
    ledger.close()
