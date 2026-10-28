import pytest

from app.engines.orchestrator import PaperOrderIntent
from app.engines.paper_ledger import SQLitePaperLedger


def _open(ledger, proposal_id, opened_at, side="LONG"):
    entry = 100.0
    intent = PaperOrderIntent(
        proposal_id=proposal_id, signal_id=f"signal-{proposal_id}", symbol="BTCUSDT",
        side=side, quantity=1.0, entry_price=entry,
        stop_loss=90.0 if side == "LONG" else 110.0,
        take_profit=120.0 if side == "LONG" else 80.0,
        recorded_at=opened_at,
    )
    assert ledger.record_intent(intent) == "RECORDED"
    assert ledger.simulate_fill(proposal_id, market_price=entry, filled_at=opened_at + 1,
                                fee_rate=0, slippage_bps=0).status == "FILLED"


def test_realized_risk_report_uses_utc_day_and_ledger_closes(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "risk.db")
    _open(ledger, "winner", 900)
    assert ledger.close_position("winner", market_price=110, closed_at=1_000,
                                 fee_rate=0, slippage_bps=0).status == "CLOSED"
    _open(ledger, "loser", 86_400)
    assert ledger.close_position("loser", market_price=90, closed_at=86_500,
                                 fee_rate=0, slippage_bps=0).status == "CLOSED"

    report = ledger.realized_risk_report(initial_equity=1_000, as_of=87_000)
    assert report.daily_realized_pnl == pytest.approx(-10)
    assert report.daily_loss == pytest.approx(10)
    assert report.ending_realized_equity == pytest.approx(1_000)
    assert report.max_drawdown_amount == pytest.approx(10)
    assert report.max_drawdown_pct == pytest.approx(1.0)
    assert report.realized_event_count == 2
    assert report.orders_authorized is False
    ledger.close()


def test_realized_risk_report_attributes_funding_to_event_day_once(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "funding-risk.db")
    _open(ledger, "funded", 900, side="LONG")
    assert ledger.record_funding_payment("funded", funding_id="fund-1", funding_rate=0.01,
                                         mark_price=100, occurred_at=1_000) == "RECORDED"
    assert ledger.close_position("funded", market_price=100, closed_at=86_500,
                                 fee_rate=0, slippage_bps=0).status == "CLOSED"

    day_one = ledger.realized_risk_report(initial_equity=1_000, as_of=2_000)
    day_two = ledger.realized_risk_report(initial_equity=1_000, as_of=87_000)
    # Long pays 1.00 funding at day one's timestamp; the close itself is flat.
    assert day_one.daily_realized_pnl == pytest.approx(-1.0)
    assert day_two.daily_realized_pnl == pytest.approx(0.0)
    assert day_two.ending_realized_equity == pytest.approx(999.0)
    ledger.close()


def test_realized_risk_report_rejects_invalid_baseline_and_time(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "invalid-risk.db")
    with pytest.raises(ValueError, match="initial_equity"):
        ledger.realized_risk_report(initial_equity=0, as_of=100)
    with pytest.raises(ValueError, match="as_of"):
        ledger.realized_risk_report(initial_equity=1_000, as_of=-1)
    ledger.close()


def test_mark_to_market_report_tracks_unrealized_pnl_and_persists_high_water(tmp_path):
    path = tmp_path / "equity.db"
    ledger = SQLitePaperLedger(path)
    _open(ledger, "long", 100, side="LONG")
    ledger.mark_positions({"BTCUSDT": 110}, observed_at=102)
    first = ledger.mark_to_market_risk_report(initial_equity=1_000, as_of=103)
    assert first.account_equity == pytest.approx(1_010)
    assert first.unrealized_pnl == pytest.approx(10)
    assert first.drawdown_pct == pytest.approx(0)
    assert first.marks_fresh is True
    ledger.close()

    recovered = SQLitePaperLedger(path)
    recovered.mark_positions({"BTCUSDT": 105}, observed_at=104)
    second = recovered.mark_to_market_risk_report(initial_equity=1_000, as_of=105)
    assert second.account_equity == pytest.approx(1_005)
    assert second.high_water_equity == pytest.approx(1_010)
    assert second.drawdown_amount == pytest.approx(5)
    assert second.drawdown_pct == pytest.approx(5 / 1_010 * 100)
    assert second.orders_authorized is False
    recovered.close()


def test_mark_to_market_report_rejects_stale_and_backwards_snapshots(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "snapshot-order.db")
    _open(ledger, "long", 100)
    stale = ledger.mark_to_market_risk_report(
        initial_equity=1_000, as_of=200, max_mark_age_seconds=30
    )
    assert stale.marks_fresh is False
    with pytest.raises(ValueError, match="backwards"):
        ledger.mark_to_market_risk_report(initial_equity=1_000, as_of=199)
    ledger.close()


def test_mark_to_market_snapshot_same_timestamp_is_idempotent_or_conflicts(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "snapshot-idempotency.db")
    _open(ledger, "long", 100)
    ledger.mark_positions({"BTCUSDT": 100}, observed_at=102)
    first = ledger.mark_to_market_risk_report(initial_equity=1_000, as_of=103)
    repeated = ledger.mark_to_market_risk_report(initial_equity=1_000, as_of=103)
    assert repeated == first
    ledger.mark_positions({"BTCUSDT": 101}, observed_at=103)
    with pytest.raises(ValueError, match="Conflicting"):
        ledger.mark_to_market_risk_report(initial_equity=1_000, as_of=103)
    ledger.close()


def test_open_position_and_emergency_stop_survive_restart_and_stale_marks_fail_closed(tmp_path):
    path = tmp_path / "restart-state.db"
    ledger = SQLitePaperLedger(path)
    _open(ledger, "survivor", 100)
    ledger.activate_emergency_stop(occurred_at=102, reason="restart matrix test")
    ledger.close()

    recovered = SQLitePaperLedger(path)
    assert len(recovered.open_positions) == 1
    assert recovered.emergency_stop_active is True
    # The stored mark is too old for this risk snapshot. Existing position state
    # is retained, but the report explicitly marks it unhealthy for new risk.
    report = recovered.mark_to_market_risk_report(
        initial_equity=1_000, as_of=200, max_mark_age_seconds=30,
    )
    assert report.marks_fresh is False
    assert report.open_position_count == 1
    assert report.orders_authorized is False
    assert recovered.emergency_stop_active is True
    recovered.close()


def test_future_mark_is_unhealthy_after_restart(tmp_path):
    path = tmp_path / "future-mark.db"
    ledger = SQLitePaperLedger(path)
    _open(ledger, "future", 100)
    ledger.mark_positions({"BTCUSDT": 101}, observed_at=110)
    ledger.close()

    recovered = SQLitePaperLedger(path)
    report = recovered.mark_to_market_risk_report(
        initial_equity=1_000, as_of=109, max_mark_age_seconds=30,
    )
    assert report.marks_fresh is False
    assert report.oldest_mark_age_seconds == pytest.approx(0)
    recovered.close()
