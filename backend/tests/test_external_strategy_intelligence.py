from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from backend.backtesting import BacktestConfig
from backend.external_strategy_intelligence import (
    ExternalPerformanceEvidence,
    ExternalRuleSpec,
    ExternalStrategyRecord,
    assess_external_strategy,
    assess_walk_forward_decay,
    evaluate_external_candidate,
    evaluate_external_rule,
    persist_external_strategy_experiment,
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
        indicators=("SMA", "ATR"),
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
        exit_policy="ONE_BAR",
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
    assert assessment.candidate.indicators == ("SMA", "ATR")
    assert assessment.calibration_status == "NOT_CALIBRATED"
    assert assessment.orders_authorized is False


def test_missing_methodology_blocks_candidate_even_when_rules_are_present():
    assessment = assess_external_strategy(record(rules=valid_rules(), methodology=""))

    assert assessment.status == "METHODOLOGY_MISSING"
    assert assessment.candidate is None


@pytest.mark.parametrize(
    ("family", "parameters", "exit_policy"),
    [
        ("SMA_CROSS", {"fast_period": 30, "slow_period": 10}, "ONE_BAR"),
        ("MOMENTUM", {"lookback": 0, "threshold_pct": 1.0}, "ONE_BAR"),
        ("UNKNOWN", {}, "ONE_BAR"),
        ("RSI_THRESHOLD", {"period": 14, "oversold": 60, "overbought": 40}, "ONE_BAR"),
    ],
)
def test_invalid_or_unsupported_rule_specs_are_rejected(family, parameters, exit_policy):
    with pytest.raises(ValueError):
        rules = ExternalRuleSpec(family=family, parameters=parameters, exit_policy=exit_policy)
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
        symbol="BTCUSDT",
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


@pytest.mark.parametrize(
    ("rule_spec", "closes", "expected"),
    [
        (ExternalRuleSpec("SMA_CROSS", {"fast_period": 5, "slow_period": 20}, "ONE_BAR"),
         [100 + index for index in range(25)], "LONG"),
        (ExternalRuleSpec("MOMENTUM", {"lookback": 5, "threshold_pct": 1.0}, "ONE_BAR"),
         [100.0] * 14 + [102.0], "LONG"),
        (ExternalRuleSpec("BREAKOUT", {"lookback": 10, "buffer_pct": 0.1}, "ONE_BAR"),
         [100.0] * 14 + [102.0], "LONG"),
        (ExternalRuleSpec("RSI_THRESHOLD", {"period": 14, "oversold": 30, "overbought": 70}, "ONE_BAR"),
         [100.0 + index for index in range(15)], "SHORT"),
    ],
)
def test_supported_external_rule_families_are_deterministic(rule_spec, closes, expected):
    start = NOW - timedelta(minutes=len(closes) - 1)
    rows = [
        Candle("BTCUSDT", start + timedelta(minutes=index), close, close * 1.01,
               close * 0.99, close, 1000.0)
        for index, close in enumerate(closes)
    ]

    assert evaluate_external_rule(rule_spec, rows) == expected


def test_external_rule_requires_an_explicit_supported_exit_policy():
    with pytest.raises(ValueError, match="exit policy"):
        ExternalRuleSpec(
            family="SMA_CROSS",
            parameters={"fast_period": 10, "slow_period": 30},
            exit_policy="TRAILING_ATR",
        )


def test_missing_market_or_timeframe_scope_does_not_create_candidate():
    item = record(rules=valid_rules())
    assessment = assess_external_strategy(replace(item, timeframes=()))

    assert assessment.status == "INCOMPLETE_SCOPE"
    assert assessment.candidate is None


def test_walk_forward_decay_uses_validation_folds_and_never_the_holdout():
    from backend.backtesting import BacktestResult
    from backend.walk_forward import evaluate_walk_forward

    rows = []
    start = NOW - timedelta(minutes=359)
    price = 100.0
    for index in range(360):
        price *= 1.0005
        rows.append(Candle(
            "BTCUSDT", start + timedelta(minutes=index), price,
            price * 1.002, price * 0.998, price, 1000.0,
        ))
    baseline = evaluate_walk_forward(
        "BTCUSDT", rows,
        config=WalkForwardConfig(
            train_bars=120, validation_bars=40, holdout_bars=40, step_bars=40,
        ),
        research_now=NOW,
    )
    folds = []
    for index, fold in enumerate(baseline.folds):
        pnl = 400.0 if index < 3 else -100.0
        strategy = replace(fold.strategy, net_pnl=pnl)
        folds.append(replace(fold, strategy=strategy))
    changed = replace(baseline, folds=tuple(folds))
    report = assess_walk_forward_decay(changed, recent_folds=2, minimum_folds=4)

    assert report.status == "RECENT_EDGE_DETERIORATED"
    assert report.change_pct_points < 0
    assert report.holdout_used is False
    assert report.orders_authorized is False


def test_walk_forward_decay_requires_enough_validation_folds():
    from backend.walk_forward import evaluate_walk_forward

    rows = []
    start = NOW - timedelta(minutes=199)
    price = 100.0
    for index in range(200):
        price *= 1.0005
        rows.append(Candle(
            "BTCUSDT", start + timedelta(minutes=index), price,
            price * 1.002, price * 0.998, price, 1000.0,
        ))
    result = evaluate_walk_forward(
        "BTCUSDT", rows,
        config=WalkForwardConfig(
            train_bars=120, validation_bars=40, holdout_bars=40, step_bars=40,
        ),
        research_now=NOW,
    )
    report = assess_walk_forward_decay(result, recent_folds=2, minimum_folds=4)

    assert report.status == "INSUFFICIENT_FOLDS"
    assert report.change_pct_points is None
    assert report.holdout_used is False
    assert report.orders_authorized is False


def test_external_strategy_evaluation_is_written_to_research_memory_without_authority():
    assessment = assess_external_strategy(record(rules=valid_rules()))
    rows = []
    start = NOW - timedelta(minutes=359)
    price = 100.0
    for index in range(360):
        price *= 1.0005
        rows.append(Candle(
            "BTCUSDT", start + timedelta(minutes=index), price,
            price * 1.002, price * 0.998, price, 1000.0,
        ))
    walk_forward = evaluate_external_candidate(
        assessment.candidate,
        rows,
        symbol="BTCUSDT",
        walk_forward_config=WalkForwardConfig(
            train_bars=120, validation_bars=40, holdout_bars=40, step_bars=40,
        ),
        backtest_config=BacktestConfig(fee_bps_per_side=4, slippage_bps_per_side=2),
        research_now=NOW,
    )

    class MemoryCapture:
        def append(self, experiment, *, idempotency_key):
            self.experiment = experiment
            self.idempotency_key = idempotency_key
            return experiment

    memory = MemoryCapture()
    result = persist_external_strategy_experiment(
        memory,
        assessment.candidate,
        walk_forward,
        dataset_start=rows[0].timestamp,
        dataset_end=rows[-1].timestamp,
        configuration_version="wf-v1",
        experiment_version=1,
        assumptions={"fee_bps_per_side": 4, "slippage_bps_per_side": 2},
        idempotency_key="alpha-experiment-v1",
    )

    assert result is memory.experiment
    assert memory.experiment.source_provenance_id == assessment.provenance_id
    assert memory.experiment.candidate_fingerprint == assessment.candidate.candidate_fingerprint
    assert memory.experiment.assumptions["orders_authorized"] is False
    assert memory.experiment.outcomes["orders_authorized"] is False
    assert memory.experiment.outcomes["holdout_used_for_selection"] is False
    assert memory.experiment.outcomes["promotion_decision"] == "NOT_PROMOTED"
    assert memory.experiment.outcomes["market_regime_evidence"]["status"] == "NOT_AVAILABLE"
    assert memory.experiment.assumptions["external_rule_spec"]["family"] == "SMA_CROSS"
    assert memory.experiment.assumptions["source_provenance"]["source_url"] == "https://example.com/public-strategy/alpha"
    assert memory.experiment.assumptions["published_performance"]["sample_size"] == 80
    assert memory.experiment.assumptions["published_performance"]["metrics"]["return_pct"] == 18.4
    assert memory.experiment.assumptions["published_performance"]["methodology"] == "Published backtest methodology v1"
    assert memory.experiment.orders_authorized is False


def test_external_rule_metadata_must_match_published_indicators():
    item = record(rules=valid_rules())
    assessment = assess_external_strategy(replace(item, indicators=("EMA", "ATR")))

    assert assessment.status == "RULE_METADATA_MISMATCH"
    assert assessment.candidate is None


def test_external_rule_spec_freezes_parameters_after_fingerprinting_inputs():
    parameters = {"fast_period": 10, "slow_period": 30}
    rules = ExternalRuleSpec("SMA_CROSS", parameters, "ONE_BAR")

    parameters["fast_period"] = 20

    assert rules.parameters["fast_period"] == 10
    with pytest.raises(TypeError):
        rules.parameters["fast_period"] = 5


def test_published_external_metrics_are_immutable_source_evidence():
    metrics = {"return_pct": 12.0}
    evidence = ExternalPerformanceEvidence(
        sample_size=50,
        backtest_start=NOW - timedelta(days=90),
        backtest_end=NOW - timedelta(days=10),
        published_metrics=metrics,
        methodology="documented",
    )

    metrics["return_pct"] = 999.0

    assert evidence.published_metrics["return_pct"] == 12.0
    with pytest.raises(TypeError):
        evidence.published_metrics["return_pct"] = 1.0
