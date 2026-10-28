from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.core.models import RiskLimits
from app.engines.paper_sizing import PaperSizingConfig, build_paper_orchestration_request
from backend.signal_watcher import MarketTick

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def make_handoff(direction="LONG", entry=100.0, stop=95.0, target=110.0):
    signal = SimpleNamespace(
        signal_id="signal-1", symbol="BTCUSDT", direction=direction,
        entry_price=entry, stop_price=stop, target_price=target,
        created_at=NOW,
    )
    validation = SimpleNamespace(status="VALID", signal_id="signal-1", symbol="BTCUSDT", direction=direction)
    return SimpleNamespace(status="VALID", orders_authorized=False, signal=signal, validation=validation)


def test_sizing_caps_quantity_by_risk_budget_and_notional():
    request = build_paper_orchestration_request(
        candidate=SimpleNamespace(symbol="BTCUSDT", direction="LONG"),
        handoff=make_handoff(), tick=MarketTick("BTCUSDT", 100, NOW), now=NOW,
        equity=1000, risk_fraction=0.01, quantity_step=0.001, min_quantity=0.001,
        limits=RiskLimits(max_position_notional=500, max_daily_loss=50, max_open_positions=3,
                          max_risk_per_trade=20), daily_pnl=0, open_positions=0,
    )
    assert request.proposal.quantity == pytest.approx(2.0)
    assert request.proposal.estimated_risk == pytest.approx(10.0)
    assert request.proposal.quantity * request.proposal.entry_price <= 500
    assert request.market.symbol == "BTCUSDT"
    assert request.watch_result is None


def test_sizing_rejects_mismatched_symbol_or_direction():
    with pytest.raises(ValueError, match="match"):
        build_paper_orchestration_request(
            candidate=SimpleNamespace(symbol="ETHUSDT", direction="LONG"),
            handoff=make_handoff(), tick=MarketTick("BTCUSDT", 100, NOW), now=NOW,
            equity=1000, risk_fraction=0.01, quantity_step=0.001, min_quantity=0.001,
            limits=RiskLimits(500, 50, 3, 20), daily_pnl=0, open_positions=0,
        )


def test_sizing_fails_closed_when_minimum_quantity_exceeds_risk_budget():
    with pytest.raises(ValueError, match="minimum quantity"):
        build_paper_orchestration_request(
            candidate=SimpleNamespace(symbol="BTCUSDT", direction="LONG"),
            handoff=make_handoff(entry=100, stop=99.99, target=100.02),
            tick=MarketTick("BTCUSDT", 100, NOW), now=NOW,
            equity=1, risk_fraction=0.001, quantity_step=1, min_quantity=1,
            limits=RiskLimits(1000, 50, 3, 20), daily_pnl=0, open_positions=0,
        )


def test_sizing_rejects_stale_tick():
    with pytest.raises(ValueError, match="stale"):
        build_paper_orchestration_request(
            candidate=SimpleNamespace(symbol="BTCUSDT", direction="LONG"), handoff=make_handoff(),
            tick=MarketTick("BTCUSDT", 100, NOW.replace(hour=11, minute=59)), now=NOW,
            equity=1000, risk_fraction=0.01, quantity_step=0.001, min_quantity=0.001,
            limits=RiskLimits(500, 50, 3, 20), daily_pnl=0, open_positions=0,
        )


def test_sizing_rejects_future_tick_and_authorization_flag():
    with pytest.raises(ValueError, match="future"):
        build_paper_orchestration_request(
            candidate=SimpleNamespace(symbol="BTCUSDT", direction="LONG"),
            handoff=make_handoff(), tick=MarketTick("BTCUSDT", 100, NOW.replace(year=2027)), now=NOW,
            equity=1000, risk_fraction=0.01, quantity_step=0.001, min_quantity=0.001,
            limits=RiskLimits(500, 50, 3, 20), daily_pnl=0, open_positions=0,
        )
    bad = make_handoff()
    object.__setattr__(bad, "orders_authorized", True)
    with pytest.raises(ValueError, match="authorization"):
        build_paper_orchestration_request(
            candidate=SimpleNamespace(symbol="BTCUSDT", direction="LONG"), handoff=bad,
            tick=MarketTick("BTCUSDT", 100, NOW), now=NOW, equity=1000, risk_fraction=0.01,
            quantity_step=0.001, min_quantity=0.001, limits=RiskLimits(500, 50, 3, 20),
            daily_pnl=0, open_positions=0,
        )
