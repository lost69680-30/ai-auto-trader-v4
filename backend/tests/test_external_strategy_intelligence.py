from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from backend.backtesting import BacktestConfig
from backend.external_strategy_intelligence import (
    ExternalPerformanceEvidence,
    ExternalRuleSpec,
    ExternalStrategyRecord,
    assess_external_strategy,
    evaluate_external_candidate,
)
from backend.walk_forward import WalkForwardConfig
from backend.strategy_research import Candle

NOW = datetime(2026, 11, 2, 18, 0, tzinfo=timezone.utc)


def record(*, sample_size=80, rules=None, methodology="Published backtest methodology v1"):
    return ExternalStrategyRecord(
        source_name="Trader.AI public leaderboard",
        source_url="https://example.com/public-strategy/alpha",
        source_record_id="alpha-123",
        title="Alpha trend strategy",
        captured_at=NOW,
        published_at=NOW - timedelta(days=30),
        market_types=("CRYPTO_SPOT",),
        symbols=("BTCUSDT",),
        timeframes=("1h",),
        indicators=("EMA", "ATR"),
        performance=ExternalPerformanceEvidence(
            sample_size=sample_size,
            backtest_start=NOW - timedelta(days=180),
            backtest_end=NOW - timedelta(days=30),
            published_metrics={"return_pct": 18.4, "max_drawdown_pct": 9.2},
            methodology=methodology,
        ),
        rules=rules,
    )


def valid_rules():
    return ExternalRuleSpec(
        family="SMA_CROSS",
        parameters={"fast_period": 10, "slow_period": 30},
    )


def test_public_metrics_without_published_rules_remain_idea_only():
    assessment = assess_external_strategy(record())

    assert assessment.status == "IDEA_ONLY"
    assert assessment.candidate is None
    assert assessment.source_name == "Trader.AI public leaderboard"
    assert assessment.provenance_id
    assert assessment.calibration_status == "NOT_CALIBRATED"
    assert assessment.orders_authorized is False


def test_low_sample_external_strategy_cannot_become_research_candidate():
    assessment = assess_external_strategy(record(sample_size=12, rules=valid_rules()))

    assert assessment.status == "INSUFFICIENT_SAMPLE"
    assert assessment.candidate is None
    assert assessment.orders_authorized is False


def test_rule_backed_strategy_with_methodology_becomes_reproducible_candidate():
    assessment = assess_external_strategy(record(rules=valid_rules()))

    assert assessment.status == "RESEARCH_CANDIDATE"
    assert assessment.candidate is not None
    assert assessment.candidate.rule_spec == valid_rules()
    assert assessment.candidate.source_provenance_id == assessment.provenance_id
    assert assessment.candidate.candidate_fingerprint
    assert assessment.calibration_status == "NOT_CALIBRATED"
    assert assessment.orders_authorized is False


def test_missing_methodology_blocks_candidate_even_when_rules_are_present():
    assessment = assess_external_strategy(record(rules=valid_rules(), methodology=""))

    assert assessment.status == "METHODOLOGY_MISSING"
    assert assessment.candidate is None


@pytest.mark.parametrize(
    ("family", "parameters"),
    [
        ("SMA_CROSS", {"fast_period": 30, "slow_period": 10}),
        ("MOMENTUM", {"lookback": 0, "threshold_pct": 1.0}),
        ("UNKNOWN", {}),
        ("RSI_THRESHOLD", {"period": 14, "oversold": 60, "overbought": 40}),
    ],
)
def test_invalid_or_unsupported_rule_specs_are_rejected(family, parameters):
    with pytest.raises(ValueError):
        rules = ExternalRuleSpec(family=family, parameters=parameters)
        assess_external_strategy(record(rules=rules))


def test_invalid_provenance_and_backtest_windows_are_rejected():
    item = record(rules=valid_rules())
    with pytest.raises(ValueError, match="source"):
        assess_external_strategy(
            replace(item, source_url="not-a-url")
        )
    with pytest.raises(ValueError, match="window"):
        assess_external_strategy(
            replace(
                item,
                performance=ExternalPerformanceEvidence(
                    sample_size=80,
                    backtest_start=NOW,
                    backtest_end=NOW - timedelta(days=1),
                    published_metrics={},
                    methodology="method",
                ),
            )
        )


def test_explicit_external_rules_are_evaluated_with_costs_and_untouched_holdout():
    assessment = assess_external_strategy(record(rules=valid_rules()))
    assert assessment.candidate is not None
    rows = []
    start = NOW - timedelta(minutes=359)
    price = 100.0
    for index in range(360):
        price *= 1.0005
        rows.append(Candle(
            "BTCUSDT", start + timedelta(minutes=index), price,
            price * 1.002, price * 0.998, price, 1000.0,
        ))

    result = evaluate_external_candidate(
        assessment.candidate,
        rows,
        walk_forward_config=WalkForwardConfig(
            train_bars=120, validation_bars=40, holdout_bars=40, step_bars=40,
        ),
        backtest_config=BacktestConfig(fee_bps_per_side=4, slippage_bps_per_side=2),
        research_now=NOW,
    )

    assert result.folds
    assert result.holdout.used_for_selection is False
    assert result.holdout.strategy.orders_authorized is False
    assert all(fold.strategy.orders_authorized is False for fold in result.folds)
    assert result.holdout.strategy.total_costs >= 0
    assert result.orders_authorized is False
