from dataclasses import replace
import sqlite3

import pytest

from app.core.models import MarketSnapshot, RiskLimits, TradeProposal
from app.engines.orchestrator import (
    OrchestrationRequest, PaperOrderIntent, TradingOrchestrator,
)
from backend.signal_validation import SignalValidationResult
from backend.signal_watcher import WatchResult
from app.engines.paper_ledger import SQLitePaperLedger


def make_intent(**changes):
    intent = PaperOrderIntent(
        proposal_id="proposal-1",
        signal_id="signal-1",
        symbol="BTCUSDT",
        side="LONG",
        quantity=0.01,
        entry_price=50_000,
        stop_loss=49_000,
        take_profit=52_000,
        recorded_at=1000,
    )
    return replace(intent, **changes)


def test_intent_is_persisted_and_restored_after_reopen(tmp_path):
    path = tmp_path / "paper.db"
    ledger = SQLitePaperLedger(path)
    assert ledger.record_intent(make_intent()) == "RECORDED"
    ledger.close()

    recovered = SQLitePaperLedger(path)
    assert len(recovered.orders) == 1
    assert recovered.orders[0].proposal_id == "proposal-1"
    assert recovered.orders[0].status == "PENDING_PAPER_SIMULATION"
    assert recovered.orders_authorized is False
    recovered.close()


def test_same_intent_is_idempotent_but_changed_reuse_conflicts(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    intent = make_intent()
    assert ledger.record_intent(intent) == "RECORDED"
    assert ledger.record_intent(intent) == "DUPLICATE"
    assert ledger.record_intent(replace(intent, quantity=0.02)) == "CONFLICT"
    assert len(ledger.orders) == 1
    ledger.close()


def test_simulated_fill_creates_fill_and_open_position_with_adverse_slippage(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    ledger.record_intent(make_intent())

    result = ledger.simulate_fill("proposal-1", market_price=50_000, filled_at=1001, fee_rate=0.001, slippage_bps=10)

    assert result.status == "FILLED"
    assert result.orders_authorized is False
    assert result.fill.price == pytest.approx(50_050)
    assert result.fill.fee == pytest.approx(0.5005)
    assert result.position.status == "OPEN"
    assert result.position.entry_price == pytest.approx(50_050)
    assert len(ledger.fills) == 1
    assert len(ledger.open_positions) == 1
    assert ledger.orders[0].status == "FILLED"
    ledger.close()


def test_fill_idempotency_survives_process_restart(tmp_path):
    path = tmp_path / "paper.db"
    ledger = SQLitePaperLedger(path)
    ledger.record_intent(make_intent())
    first = ledger.simulate_fill("proposal-1", market_price=50_000, filled_at=1001)
    assert first.status == "FILLED"
    ledger.close()

    recovered = SQLitePaperLedger(path)
    repeated = recovered.simulate_fill("proposal-1", market_price=50_000, filled_at=1001)
    conflicting = recovered.simulate_fill("proposal-1", market_price=50_100, filled_at=1002)
    assert repeated.status == "ALREADY_FILLED"
    assert conflicting.status == "FILL_CONFLICT"
    assert len(recovered.fills) == 1
    assert len(recovered.open_positions) == 1
    recovered.close()


def test_short_fill_and_mark_to_market_use_short_pnl_sign(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    ledger.record_intent(make_intent(side="SHORT", entry_price=50_000, stop_loss=51_000, take_profit=48_000))
    ledger.simulate_fill("proposal-1", market_price=50_000, filled_at=1001, fee_rate=0, slippage_bps=0)

    marked = ledger.mark_positions({"BTCUSDT": 49_000}, observed_at=1002)
    assert marked[0].unrealized_pnl == pytest.approx(10)
    with pytest.raises(ValueError, match="older"):
        ledger.mark_positions({"BTCUSDT": 51_000}, observed_at=1001)
    assert ledger.positions[0].mark_price == pytest.approx(49_000)
    ledger.close()


def test_closing_position_persists_net_realized_pnl_and_is_idempotent(tmp_path):
    path = tmp_path / "paper.db"
    ledger = SQLitePaperLedger(path)
    ledger.record_intent(make_intent())
    ledger.simulate_fill("proposal-1", market_price=50_000, filled_at=1001, fee_rate=0.001, slippage_bps=0)

    result = ledger.close_position("proposal-1", market_price=51_000, closed_at=1002, fee_rate=0.001, slippage_bps=0)
    assert result.status == "CLOSED"
    assert result.position.status == "CLOSED"
    assert result.position.realized_pnl == pytest.approx(8.99)
    assert result.position.unrealized_pnl == 0
    assert result.position.close_price == pytest.approx(51_000)
    assert ledger.open_positions == ()
    assert ledger.orders[0].status == "CLOSED"
    repeated = ledger.close_position("proposal-1", market_price=51_000, closed_at=1002, fee_rate=0.001, slippage_bps=0)
    assert repeated.status == "ALREADY_CLOSED"
    ledger.close()

    recovered = SQLitePaperLedger(path)
    assert recovered.positions[0].status == "CLOSED"
    assert recovered.positions[0].realized_pnl == pytest.approx(8.99)
    recovered.close()


def test_invalid_fill_and_unknown_order_do_not_change_ledger(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    assert ledger.simulate_fill("missing", market_price=50_000, filled_at=1001).status == "NOT_FOUND"
    ledger.record_intent(make_intent())
    for kwargs in (
        {"market_price": 0, "filled_at": 1001},
        {"market_price": float("nan"), "filled_at": 1001},
        {"market_price": True, "filled_at": 1001},
        {"market_price": 50_000, "filled_at": 1001, "fee_rate": -0.1},
        {"market_price": 50_000, "filled_at": 1001, "slippage_bps": 10_000},
        {"market_price": 50_000, "filled_at": 999},
    ):
        with pytest.raises(ValueError):
            ledger.simulate_fill("proposal-1", **kwargs)
    assert ledger.fills == ()
    assert ledger.positions == ()
    assert ledger.orders[0].status == "PENDING_PAPER_SIMULATION"
    ledger.close()


def test_orchestrator_can_record_to_durable_ledger(tmp_path):
    # The same ledger contract is used by the orchestrator; no exchange client exists here.
    proposal = TradeProposal("proposal-1", "BTCUSDT", "LONG", 0.01, 50_000, 49_000, 52_000, 10, 1000)
    request = OrchestrationRequest(
        proposal=proposal,
        market=MarketSnapshot("BTCUSDT", 50_000, 1000, True),
        validation=SignalValidationResult(
            signal_id="signal-1", symbol="BTCUSDT", direction="LONG", status="VALID",
            reasons=(), agreeing_strategies=("trend",), opposing_strategies=(),
            gross_reward_risk=2, net_reward_risk=1.8,
        ),
        watch_result=WatchResult("watch-1", "signal-1", "BTCUSDT", "TRIGGERED", "test", True, 50_000),
        limits=RiskLimits(1000, 100, 3, 20), daily_pnl=0, open_positions=0, now=1000, initial_equity=10_000,
    )

    path = tmp_path / "orchestrated.db"
    ledger = SQLitePaperLedger(path)
    result = TradingOrchestrator(ledger).process(request)
    assert result.status == "PAPER_ORDER_RECORDED"
    ledger.close()

    recovered = SQLitePaperLedger(path)
    assert len(recovered.orders) == 1
    assert recovered.fills == ()
    recovered.close()


def test_fill_is_blocked_if_slippage_puts_price_outside_protection_geometry(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    ledger.record_intent(make_intent())

    result = ledger.simulate_fill(
        "proposal-1", market_price=52_000, filled_at=1001, fee_rate=0, slippage_bps=0
    )

    assert result.status == "FILL_BLOCKED_BY_PROTECTION"
    assert ledger.fills == ()
    assert ledger.open_positions == ()
    assert ledger.orders[0].status == "PENDING_PAPER_SIMULATION"
    ledger.close()


def test_close_before_last_accepted_mark_is_rejected_atomically(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    ledger.record_intent(make_intent())
    ledger.simulate_fill("proposal-1", market_price=50_000, filled_at=1001, fee_rate=0, slippage_bps=0)
    ledger.mark_positions({"BTCUSDT": 50_100}, observed_at=1003)

    with pytest.raises(ValueError, match="last accepted mark"):
        ledger.close_position("proposal-1", market_price=50_200, closed_at=1002, fee_rate=0, slippage_bps=0)

    assert ledger.open_positions[0].mark_price == pytest.approx(50_100)
    assert ledger.audit_events[-1].event_type == "PAPER_POSITION_MARKED"
    ledger.close()


def test_audit_events_survive_restart(tmp_path):
    path = tmp_path / "paper.db"
    ledger = SQLitePaperLedger(path)
    ledger.record_intent(make_intent())
    ledger.simulate_fill("proposal-1", market_price=50_000, filled_at=1001, fee_rate=0, slippage_bps=0)
    assert [event.event_type for event in ledger.audit_events] == [
        "PAPER_ORDER_INTENT_RECORDED", "PAPER_FILL_SIMULATED"
    ]
    ledger.close()

    recovered = SQLitePaperLedger(path)
    assert [event.event_type for event in recovered.audit_events] == [
        "PAPER_ORDER_INTENT_RECORDED", "PAPER_FILL_SIMULATED"
    ]
    recovered.close()


def test_short_fill_and_close_apply_adverse_slippage_on_both_sides(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    ledger.record_intent(make_intent(side="SHORT", entry_price=50_000, stop_loss=51_000, take_profit=48_000))

    filled = ledger.simulate_fill(
        "proposal-1", market_price=50_000, filled_at=1001, fee_rate=0, slippage_bps=10
    )
    closed = ledger.close_position(
        "proposal-1", market_price=49_000, closed_at=1002, fee_rate=0, slippage_bps=10
    )

    assert filled.fill.price == pytest.approx(49_950)
    assert closed.position.close_price == pytest.approx(49_049)
    assert closed.position.realized_pnl == pytest.approx(9.01)
    ledger.close()


def test_order_authorization_is_read_only_and_false(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    assert ledger.orders_authorized is False
    with pytest.raises(AttributeError):
        ledger.orders_authorized = True
    ledger.close()


def test_file_backed_ledger_creates_missing_parent_directories(tmp_path):
    database = tmp_path / "data" / "paper" / "account.sqlite3"
    ledger = SQLitePaperLedger(database)
    ledger.record_intent(make_intent())
    ledger.close()

    assert database.is_file()
    recovered = SQLitePaperLedger(database)
    assert len(recovered.orders) == 1
    recovered.close()


def test_pending_paper_intents_are_durably_cancelled_when_trigger_state_is_not_recoverable(tmp_path):
    path = tmp_path / "restart.db"
    ledger = SQLitePaperLedger(path)
    assert ledger.record_intent(make_intent(recorded_at=1000)) == "RECORDED"
    assert ledger.cancel_pending_paper_orders(
        occurred_at=1001, reason="restart requires fresh signal state",
    ) == ("proposal-1",)
    assert ledger.orders[0].status == "CANCELLED"
    result = ledger.simulate_fill(
        "proposal-1", market_price=50_100, filled_at=1002,
    )
    assert result.status == "ORDER_CANCELLED"
    assert ledger.orders_authorized is False
    assert any(event.event_type == "PAPER_ORDER_CANCELLED_ON_SERVICE_START" for event in ledger.audit_events)
    ledger.close()


def test_pending_paper_cancellation_is_idempotent_and_rejects_time_reversal(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "restart-idempotent.db")
    assert ledger.record_intent(make_intent(recorded_at=1000)) == "RECORDED"
    with pytest.raises(ValueError, match="cannot precede"):
        ledger.cancel_pending_paper_orders(occurred_at=999, reason="restart")
    assert ledger.orders[0].status == "PENDING_PAPER_SIMULATION"
    assert ledger.cancel_pending_paper_orders(occurred_at=1001, reason="restart") == ("proposal-1",)
    assert ledger.cancel_pending_paper_orders(occurred_at=1002, reason="second restart") == ()
    ledger.close()


def test_funding_payment_is_signed_idempotent_and_included_in_pnl(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "funding.db")
    ledger.record_intent(make_intent())
    ledger.simulate_fill("proposal-1", market_price=50_000, filled_at=1001, fee_rate=0, slippage_bps=0)

    assert ledger.record_funding_payment(
        "proposal-1", funding_id="BTC-1008", funding_rate=0.001,
        mark_price=50_000, occurred_at=1008,
    ) == "RECORDED"
    # Long pays 0.001 * 0.01 * 50,000 = 0.5.
    assert ledger.positions[0].funding_pnl == pytest.approx(-0.5)
    assert ledger.positions[0].total_pnl == pytest.approx(-0.5)
    assert ledger.record_funding_payment(
        "proposal-1", funding_id="BTC-1008", funding_rate=0.001,
        mark_price=50_000, occurred_at=1008,
    ) == "DUPLICATE"
    assert ledger.record_funding_payment(
        "proposal-1", funding_id="BTC-1008", funding_rate=0.002,
        mark_price=50_000, occurred_at=1008,
    ) == "CONFLICT"
    closed = ledger.close_position("proposal-1", market_price=50_000, closed_at=1010, fee_rate=0, slippage_bps=0)
    assert closed.position.realized_pnl == pytest.approx(-0.5)
    assert closed.position.total_pnl == pytest.approx(-0.5)
    ledger.close()


def test_short_receives_positive_funding_and_funding_persists_after_restart(tmp_path):
    path = tmp_path / "funding-short.db"
    ledger = SQLitePaperLedger(path)
    ledger.record_intent(make_intent(side="SHORT", entry_price=50_000, stop_loss=51_000, take_profit=48_000))
    ledger.simulate_fill("proposal-1", market_price=50_000, filled_at=1001, fee_rate=0, slippage_bps=0)
    assert ledger.record_funding_payment(
        "proposal-1", funding_id="BTC-1008", funding_rate=0.001,
        mark_price=50_000, occurred_at=1008,
    ) == "RECORDED"
    assert ledger.positions[0].funding_pnl == pytest.approx(0.5)
    ledger.close()

    recovered = SQLitePaperLedger(path)
    assert recovered.positions[0].funding_pnl == pytest.approx(0.5)
    assert len(recovered.funding_events) == 1
    recovered.close()


def test_funding_rejects_invalid_time_and_unknown_position(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "funding-invalid.db")
    assert ledger.record_funding_payment(
        "missing", funding_id="event-1", funding_rate=0.001,
        mark_price=50_000, occurred_at=1000,
    ) == "POSITION_NOT_FOUND"
    ledger.record_intent(make_intent())
    ledger.simulate_fill("proposal-1", market_price=50_000, filled_at=1001, fee_rate=0, slippage_bps=0)
    with pytest.raises(ValueError, match="predate"):
        ledger.record_funding_payment(
            "proposal-1", funding_id="event-early", funding_rate=0.001,
            mark_price=50_000, occurred_at=1000,
        )
    with pytest.raises(ValueError):
        ledger.record_funding_payment(
            "proposal-1", funding_id="event-nan", funding_rate=float("nan"),
            mark_price=50_000, occurred_at=1008,
        )
    assert ledger.funding_events == ()
    ledger.close()


def test_concurrent_duplicate_intent_submission_is_idempotent(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    ledger = SQLitePaperLedger(tmp_path / "concurrent-intent.db")
    intent = make_intent()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: ledger.record_intent(intent), range(32)))
    assert results.count("RECORDED") == 1
    assert results.count("DUPLICATE") == 31
    assert len(ledger.orders) == 1
    assert ledger.orders_authorized is False
    ledger.close()


def test_emergency_stop_and_open_positions_survive_reopen_and_block_fill(tmp_path):
    path = tmp_path / "stop-recovery.db"
    ledger = SQLitePaperLedger(path)
    ledger.record_intent(make_intent())
    ledger.simulate_fill("proposal-1", market_price=50_000, filled_at=1001, fee_rate=0, slippage_bps=0)
    ledger.record_intent(make_intent(proposal_id="proposal-2", signal_id="signal-2", recorded_at=1002))
    cancelled = ledger.activate_emergency_stop(occurred_at=1003, reason="recovery test")
    assert cancelled == ("proposal-2",)
    ledger.close()

    recovered = SQLitePaperLedger(path)
    assert recovered.emergency_stop_active is True
    assert recovered.emergency_stop_reason == "recovery test"
    assert len(recovered.open_positions) == 1
    assert recovered.simulate_fill("proposal-2", market_price=50_000, filled_at=1004).status == "EMERGENCY_STOP_ACTIVE"
    assert recovered.record_intent(make_intent(proposal_id="proposal-3", signal_id="signal-3", recorded_at=1005)) == "BLOCKED_BY_EMERGENCY_STOP"
    recovered.close()


def test_concurrent_duplicate_intent_submission_across_independent_connections(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / "multi-connection.db"
    seed = SQLitePaperLedger(path)
    seed.close()

    def submit(_):
        with SQLitePaperLedger(path) as ledger:
            return ledger.record_intent(make_intent())

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(submit, range(32)))
    assert results.count("RECORDED") == 1
    assert results.count("DUPLICATE") == 31
    with SQLitePaperLedger(path) as ledger:
        assert len(ledger.orders) == 1
        assert ledger.orders_authorized is False


def test_ledger_exposes_integrity_check_and_rejects_corrupt_database(tmp_path):
    valid_path = tmp_path / "valid.sqlite3"
    with SQLitePaperLedger(valid_path) as ledger:
        assert ledger.verify_integrity() == "ok"

    corrupt_path = tmp_path / "corrupt.sqlite3"
    corrupt_path.write_bytes(b"not a valid sqlite database")
    with pytest.raises(sqlite3.DatabaseError):
        SQLitePaperLedger(corrupt_path)
