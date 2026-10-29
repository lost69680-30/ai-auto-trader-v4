from datetime import datetime, timedelta, timezone

import pytest

from backend.strategy_research import Candle, ResearchConfig, research_strategies

NOW = datetime(2026, 10, 10, 7, 0, tzinfo=timezone.utc)


def candles_from_closes(closes, *, start=None, volume=100.0):
    if start is None:
        start = NOW - timedelta(minutes=len(closes) - 1)
    candles = []
    for i, close in enumerate(closes):
        candles.append(Candle(
            symbol="BTCUSDT",
            timestamp=start + timedelta(minutes=i),
            open=close,
            high=close * 1.002,
            low=close * 0.998,
            close=close,
            volume=volume,
        ))
    return candles


def test_insufficient_history_returns_no_strategy_signal():
    result = research_strategies("BTCUSDT", candles_from_closes([100, 101, 102]), now=NOW)
    assert result.decision == "NO_TRADE"
    assert result.assessments
    assert all(a.direction == "NEUTRAL" for a in result.assessments)
    assert result.orders_authorized is False


def test_strong_rising_series_can_surface_trend_research_candidate():
    closes = [100 + i * 1.2 for i in range(40)]
    result = research_strategies("BTCUSDT", candles_from_closes(closes), now=NOW)
    trend = next(a for a in result.assessments if a.strategy == "trend_following")
    assert trend.direction == "LONG"
    assert result.decision == "REVIEW_LONG"
    assert result.orders_authorized is False


def test_flat_market_does_not_force_a_trade():
    result = research_strategies("BTCUSDT", candles_from_closes([100.0] * 40), now=NOW)
    assert result.decision == "NO_TRADE"
    assert all(a.direction == "NEUTRAL" for a in result.assessments)


def test_stale_or_invalid_candles_fail_closed():
    stale = candles_from_closes([100 + i for i in range(40)], start=NOW - timedelta(hours=2))
    result = research_strategies("BTCUSDT", stale, now=NOW)
    assert result.decision == "NO_TRADE"
    assert "stale" in result.reasons[0].lower()

    invalid = candles_from_closes([100 + i for i in range(40)])
    invalid[-1] = Candle("BTCUSDT", NOW, 1, 1, 2, 1, 10)
    result = research_strategies("BTCUSDT", invalid, now=NOW)
    assert result.decision == "NO_TRADE"
    assert result.reasons


def test_mixed_symbol_or_duplicate_timestamps_are_rejected():
    mixed = candles_from_closes([100 + i for i in range(40)])
    mixed[-1] = Candle("ETHUSDT", mixed[-1].timestamp, 1, 2, 0.5, 1.5, 100)
    assert research_strategies("BTCUSDT", mixed, now=NOW).decision == "NO_TRADE"

    duplicate = candles_from_closes([100 + i for i in range(40)])
    duplicate[-1] = duplicate[-2]
    assert research_strategies("BTCUSDT", duplicate, now=NOW).decision == "NO_TRADE"


def test_config_validation_and_timezone_requirement():
    with pytest.raises(ValueError):
        ResearchConfig(min_candles=2)
    with pytest.raises(ValueError):
        research_strategies("BTCUSDT", [], now=datetime(2026, 10, 10))


def test_result_is_deterministic_and_never_authorizes_orders():
    candles = candles_from_closes([100 + i * 0.8 for i in range(40)])
    first = research_strategies("BTCUSDT", candles, now=NOW)
    second = research_strategies("BTCUSDT", candles, now=NOW)
    assert first == second
    assert not first.orders_authorized
    assert all(a.confidence_score <= 100 for a in first.assessments)


def test_signal_activity_diagnostics_surface_strong_trend_without_authorizing_orders():
    from backend.strategy_diagnostics import diagnose_strategy_signal_activity

    rows = candles_from_closes([100 + i * 1.2 for i in range(40)])
    report = diagnose_strategy_signal_activity(
        "BTCUSDT", rows, signal_start_index=29, signal_end_index_exclusive=40
    )

    assert report["evaluated_signal_candles"] == 11
    assert report["decision_counts"]["REVIEW_LONG"] > 0
    assert report["eligible_consensus_candles"]["LONG"] > 0
    assert report["orders_authorized"] is False


def test_signal_activity_diagnostics_do_not_create_consensus_in_flat_market():
    from backend.strategy_diagnostics import diagnose_strategy_signal_activity

    rows = candles_from_closes([100.0] * 40)
    report = diagnose_strategy_signal_activity(
        "BTCUSDT", rows, signal_start_index=29, signal_end_index_exclusive=40
    )

    assert report["eligible_consensus_candles"] == {"LONG": 0, "SHORT": 0}
    assert report["qualified_same_direction_count_distribution"]["0"] == 11
    assert report["orders_authorized"] is False



def test_score_threshold_sweep_is_diagnostic_only_and_never_authorizes_orders():
    from backend.strategy_diagnostics import diagnose_strategy_threshold_sweep

    rows = candles_from_closes([100 + i * 0.1 for i in range(40)])
    report = diagnose_strategy_threshold_sweep(
        "BTCUSDT", rows, signal_start_index=29, signal_end_index_exclusive=40,
        thresholds=(0.0, 20.0, 40.0, 60.0),
    )

    assert report["evaluated_signal_candles"] == 11
    assert [item["minimum_score"] for item in report["thresholds"]] == [0.0, 20.0, 40.0, 60.0]
    assert all(item["orders_authorized"] is False for item in report["thresholds"])
    assert report["orders_authorized"] is False
    counts = [item["consensus_candles"] for item in report["thresholds"]]
    assert all(counts[index]["LONG"] >= counts[index + 1]["LONG"]
               for index in range(len(counts) - 1))
    assert all(counts[index]["SHORT"] >= counts[index + 1]["SHORT"]
               for index in range(len(counts) - 1))
