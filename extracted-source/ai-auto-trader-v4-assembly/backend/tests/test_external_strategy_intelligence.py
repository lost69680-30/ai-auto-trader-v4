from datetime import datetime, timedelta, timezone

import pytest

from backend.external_strategy_intelligence import (
    ExternalPerformanceEvidence,
    ExternalRuleSpec,
    ExternalStrategyRecord,
    assess_external_strategy,
)

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
    "rules",
    [
        ExternalRuleSpec(family="SMA_CROSS", parameters={"fast_period": 30, "slow_period": 10}),
        ExternalRuleSpec(family="MOMENTUM", parameters={"lookback": 0, "threshold_pct": 1.0}),
        ExternalRuleSpec(family="UNKNOWN", parameters={}),
        ExternalRuleSpec(family="RSI_THRESHOLD", parameters={"period": 14, "oversold": 60, "overbought": 40}),
    ],
)
def test_invalid_or_unsupported_rule_specs_are_rejected(rules):
    with pytest.raises(ValueError):
        assess_external_strategy(record(rules=rules))


def test_invalid_provenance_and_backtest_windows_are_rejected():
    item = record(rules=valid_rules())
    with pytest.raises(ValueError, match="source"):
        assess_external_strategy(
            ExternalStrategyRecord(
                **{**item.__dict__, "source_url": "not-a-url"}
            )
        )
    with pytest.raises(ValueError, match="window"):
        assess_external_strategy(
            ExternalStrategyRecord(
                **{
                    **item.__dict__,
                    "performance": ExternalPerformanceEvidence(
                        sample_size=80,
                        backtest_start=NOW,
                        backtest_end=NOW - timedelta(days=1),
                        published_metrics={},
                        methodology="method",
                    ),
                }
            )
        )
