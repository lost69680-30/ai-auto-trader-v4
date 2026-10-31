from datetime import datetime, timedelta, timezone

import pytest

from backend.adaptive_strategy import (
    AdaptiveSelectionConfig,
    MarketRegime,
    StrategyKind,
    StrategyProposal,
    TimeframeState,
    select_adaptive_strategy,
)

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


def timeframe(interval, direction="BULLISH", *, age=10, healthy=True, closed=True, conflicts=()):
    return TimeframeState(
        interval=interval,
        direction=direction,
        observed_at=NOW - timedelta(seconds=age),
        healthy=healthy,
        closed=closed,
        conflicts=tuple(conflicts),
    )


def proposal(strategy, *, score=80, direction="LONG", eligible=True, cost=5, cap=20):
    return StrategyProposal(
        strategy=strategy,
        direction=direction,
        eligible=eligible,
        ranking_score=score,
        estimated_round_trip_cost_bps=cost,
        max_round_trip_cost_bps=cap,
        evidence=("test evidence",),
    )


def context(*rows, regime=MarketRegime.TRENDING_UP):
    return {row.interval: row for row in rows}


def test_empty_candidates_return_wait():
    result = select_adaptive_strategy(
        symbol="BTCUSDT", regime=MarketRegime.TRENDING_UP,
        timeframes=context(), proposals=(), now=NOW,
    )
    assert result.status == "WAIT"
    assert result.selected_strategy is None
    assert result.orders_authorized is False


def test_selects_highest_ranked_eligible_strategy_with_required_timeframes():
    rows = context(
        timeframe("1m"), timeframe("5m"), timeframe("15m"),
        timeframe("1h"), timeframe("4h"),
    )
    result = select_adaptive_strategy(
        symbol="BTCUSDT", regime=MarketRegime.TRENDING_UP, timeframes=rows,
        proposals=(proposal(StrategyKind.SCALPING, score=75),
                   proposal(StrategyKind.DAY, score=88)),
        now=NOW,
    )
    assert result.status == "SELECTED"
    assert result.selected_strategy is StrategyKind.DAY
    assert result.direction == "LONG"
    assert result.orders_authorized is False


def test_missing_or_stale_required_timeframe_disqualifies_candidate():
    rows = context(timeframe("1m"), timeframe("5m"), timeframe("15m"),
                   timeframe("1h", age=1000))
    result = select_adaptive_strategy(
        symbol="BTCUSDT", regime=MarketRegime.TRENDING_UP, timeframes=rows,
        proposals=(proposal(StrategyKind.SCALPING),), now=NOW,
        config=AdaptiveSelectionConfig(max_timeframe_age_seconds=120),
    )
    assert result.status == "WAIT"
    assert result.selected_strategy is None
    assert "1h" in " ".join(result.reasons)


def test_unclosed_or_unhealthy_timeframe_fails_closed():
    rows = context(timeframe("1m"), timeframe("5m", healthy=False),
                   timeframe("15m"), timeframe("1h"))
    result = select_adaptive_strategy(
        symbol="BTCUSDT", regime=MarketRegime.TRENDING_UP, timeframes=rows,
        proposals=(proposal(StrategyKind.SCALPING),), now=NOW,
    )
    assert result.status == "WAIT"
    assert result.orders_authorized is False


def test_cost_above_cap_disqualifies_candidate():
    rows = context(timeframe("1m"), timeframe("5m"), timeframe("15m"), timeframe("1h"))
    result = select_adaptive_strategy(
        symbol="BTCUSDT", regime=MarketRegime.TRENDING_UP, timeframes=rows,
        proposals=(proposal(StrategyKind.SCALPING, cost=25, cap=20),), now=NOW,
    )
    assert result.status == "WAIT"
    assert any("cost" in reason.lower() for reason in result.reasons)


def test_conflicting_required_timeframe_blocks_candidate():
    rows = context(timeframe("1m"), timeframe("5m"), timeframe("15m"),
                   timeframe("1h", conflicts=("directional conflict",)))
    result = select_adaptive_strategy(
        symbol="BTCUSDT", regime=MarketRegime.TRENDING_UP, timeframes=rows,
        proposals=(proposal(StrategyKind.SCALPING),), now=NOW,
    )
    assert result.status == "WAIT"
    assert any("conflict" in reason.lower() for reason in result.reasons)


def test_long_term_strategy_is_analysis_only_never_execution_proposal():
    rows = context(timeframe("1w"), timeframe("1M"), timeframe("1Y"))
    result = select_adaptive_strategy(
        symbol="BTCUSDT", regime=MarketRegime.TRENDING_UP, timeframes=rows,
        proposals=(proposal(StrategyKind.LONG_TERM_ANALYSIS),), now=NOW,
    )
    assert result.status == "ANALYSIS_ONLY"
    assert result.selected_strategy is StrategyKind.LONG_TERM_ANALYSIS
    assert result.analysis_only is True
    assert result.direction == "NONE"
    assert result.orders_authorized is False


@pytest.mark.parametrize("field,value", [
    ("ranking_score", float("nan")),
    ("estimated_round_trip_cost_bps", float("inf")),
    ("max_round_trip_cost_bps", float("nan")),
])
def test_non_finite_proposal_values_are_rejected(field, value):
    kwargs = dict(strategy=StrategyKind.SCALPING, direction="LONG", eligible=True,
                  ranking_score=80, estimated_round_trip_cost_bps=5,
                  max_round_trip_cost_bps=20, evidence=("evidence",))
    kwargs[field] = value
    with pytest.raises(ValueError):
        StrategyProposal(**kwargs)


def test_uncertain_regime_waits_even_when_proposal_is_eligible():
    rows = context(timeframe("1w"), timeframe("1M"), timeframe("1Y"))
    result = select_adaptive_strategy(
        symbol="BTCUSDT", regime=MarketRegime.UNCERTAIN, timeframes=rows,
        proposals=(proposal(StrategyKind.LONG_TERM_ANALYSIS),), now=NOW,
    )
    assert result.status == "WAIT"
    assert result.selected_strategy is None
    assert result.orders_authorized is False


def test_future_timeframe_observation_is_rejected():
    rows = context(timeframe("1m"), timeframe("5m"), timeframe("15m"),
                   TimeframeState("1h", "BULLISH", NOW + timedelta(seconds=30), True, True))
    result = select_adaptive_strategy(
        symbol="BTCUSDT", regime=MarketRegime.TRENDING_UP, timeframes=rows,
        proposals=(proposal(StrategyKind.SCALPING),), now=NOW,
    )
    assert result.status == "WAIT"
    assert result.orders_authorized is False


def test_adaptive_selector_matches_deployment_package_copy():
    from pathlib import Path

    root = Path(__file__).parents[1]
    source = root / "backend" / "adaptive_strategy.py"
    packaged = root / "extracted-source" / "ai-auto-trader-v4-assembly" / "backend" / "adaptive_strategy.py"
    assert source.read_text(encoding="utf-8") == packaged.read_text(encoding="utf-8")
