from dataclasses import replace
import math

import pytest

from app.core.models import MarketSnapshot, RiskLimits, TradeProposal
from app.core.risk_guardian import evaluate_proposal


@pytest.fixture
def valid_case():
    proposal = TradeProposal(
        proposal_id="safe-1",
        symbol="BTCUSDT",
        side="LONG",
        quantity=0.01,
        entry_price=50_000,
        stop_loss=49_000,
        take_profit=52_000,
        estimated_risk=10,
        signal_timestamp=1_000,
    )
    market = MarketSnapshot("BTCUSDT", 50_000, 1_000, True)
    limits = RiskLimits(1_000, 100, 3, 20, max_signal_age_seconds=60)
    return proposal, market, limits


def decide(proposal, market, limits, *, daily_pnl=0, open_positions=0, now=1_000):
    return evaluate_proposal(
        proposal, market, limits, daily_pnl=daily_pnl,
        open_positions=open_positions, now=now,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("estimated_risk", math.nan),
        ("estimated_risk", math.inf),
        ("signal_timestamp", math.nan),
        ("quantity", math.nan),
        ("entry_price", math.inf),
    ],
)
def test_non_finite_proposal_values_fail_closed(valid_case, field, value):
    proposal, market, limits = valid_case
    result = decide(replace(proposal, **{field: value}), market, limits)
    assert result.approved is False
    assert result.code == "INVALID_INPUT"


@pytest.mark.parametrize(
    "field,value",
    [("observed_at", math.nan), ("mark_price", math.inf)],
)
def test_non_finite_market_values_fail_closed(valid_case, field, value):
    proposal, market, limits = valid_case
    result = decide(proposal, replace(market, **{field: value}), limits)
    assert result.approved is False
    assert result.code == "INVALID_INPUT"


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_position_notional", math.nan),
        ("max_daily_loss", math.inf),
        ("max_risk_per_trade", math.nan),
        ("max_signal_age_seconds", math.nan),
    ],
)
def test_non_finite_risk_limits_fail_closed(valid_case, field, value):
    proposal, market, limits = valid_case
    result = decide(proposal, market, replace(limits, **{field: value}))
    assert result.approved is False
    assert result.code == "INVALID_INPUT"


def test_non_finite_daily_pnl_fails_closed(valid_case):
    proposal, market, limits = valid_case
    result = decide(proposal, market, limits, daily_pnl=math.nan)
    assert result.approved is False
    assert result.code == "INVALID_INPUT"


def test_risk_gate_recomputes_stop_distance_risk_instead_of_trusting_underestimate(valid_case):
    proposal, market, limits = valid_case
    understated = replace(proposal, quantity=0.02, estimated_risk=1)
    limits = replace(limits, max_risk_per_trade=15)
    result = decide(understated, market, limits)
    assert result.approved is False
    assert result.code == "TRADE_RISK_LIMIT"


def test_unrepresentably_large_integer_fails_closed(valid_case):
    proposal, market, limits = valid_case
    result = decide(replace(proposal, quantity=10**1000), market, limits)
    assert result.approved is False
    assert result.code == "INVALID_INPUT"
