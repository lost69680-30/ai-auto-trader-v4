"""Read-only diagnostics for explaining strategy signal activation.

This module measures whether the existing research filters would surface candidates.
It does not change strategy thresholds, evaluate profitability, or authorize orders.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Iterable

from backend.strategy_research import Candle, ResearchConfig, research_strategies


def diagnose_strategy_signal_activity(
    symbol: str,
    candles: Iterable[Candle],
    *,
    signal_start_index: int,
    signal_end_index_exclusive: int,
    research_config: ResearchConfig | None = None,
    minimum_signal_score: float = 60.0,
) -> dict[str, object]:
    """Count existing strategy decisions over an index range of signal candles.

    Indices refer to candles that generate the signal, not the next-open entry candle.
    Each assessment uses only candles up to and including its own signal candle.
    The function is deliberately research-only and never places or authorizes orders.
    """
    if not math.isfinite(minimum_signal_score) or not 0 <= minimum_signal_score <= 100:
        raise ValueError("minimum_signal_score must be between 0 and 100")
    config = research_config or ResearchConfig()
    rows = list(candles)
    if not symbol or not rows:
        raise ValueError("symbol and candle data are required")
    if signal_start_index < config.min_candles - 1:
        raise ValueError("signal range must include enough prior candles for strategy warmup")
    if signal_end_index_exclusive > len(rows):
        raise ValueError("signal range ends beyond available candle data")
    if signal_start_index < 0 or signal_end_index_exclusive <= signal_start_index:
        raise ValueError("signal range must be a non-empty valid index range")

    decision_counts: Counter[str] = Counter()
    qualified_direction_counts: Counter[str] = Counter()
    aligned_count_distribution: Counter[int] = Counter()
    strategy_counts: dict[str, Counter[str]] = {}
    eligible_consensus = Counter({"LONG": 0, "SHORT": 0})
    evaluated = 0

    for index in range(signal_start_index, signal_end_index_exclusive):
        window_start = max(0, index - config.min_candles + 1)
        window = rows[window_start:index + 1]
        if len(window) < config.min_candles:
            raise ValueError("signal range does not provide complete strategy warmup")
        signal = research_strategies(
            symbol, window, now=window[-1].timestamp, config=config
        )
        evaluated += 1
        decision_counts[signal.decision] += 1

        qualified = {"LONG": 0, "SHORT": 0}
        for assessment in signal.assessments:
            stats = strategy_counts.setdefault(
                assessment.strategy,
                Counter({"LONG": 0, "SHORT": 0, "NEUTRAL": 0,
                         "qualified_LONG": 0, "qualified_SHORT": 0}),
            )
            stats[assessment.direction] += 1
            if (assessment.direction in qualified
                    and assessment.confidence_score >= minimum_signal_score):
                qualified[assessment.direction] += 1
                stats[f"qualified_{assessment.direction}"] += 1

        qualified_direction_counts["LONG"] += qualified["LONG"]
        qualified_direction_counts["SHORT"] += qualified["SHORT"]
        max_same_direction = max(qualified["LONG"], qualified["SHORT"])
        aligned_count_distribution[max_same_direction] += 1

        # Match the backtest's requirement: strategy decision and at least two
        # same-direction assessments meeting the backtest score threshold.
        if signal.decision == "REVIEW_LONG" and qualified["LONG"] >= 2:
            eligible_consensus["LONG"] += 1
        elif signal.decision == "REVIEW_SHORT" and qualified["SHORT"] >= 2:
            eligible_consensus["SHORT"] += 1

    return {
        "signal_start_index": signal_start_index,
        "signal_end_index_exclusive": signal_end_index_exclusive,
        "evaluated_signal_candles": evaluated,
        "decision_counts": {
            decision: decision_counts[decision]
            for decision in ("NO_TRADE", "REVIEW_LONG", "REVIEW_SHORT")
        },
        "qualified_assessment_counts": {
            "LONG": qualified_direction_counts["LONG"],
            "SHORT": qualified_direction_counts["SHORT"],
        },
        "qualified_same_direction_count_distribution": {
            str(count): aligned_count_distribution[count] for count in range(5)
        },
        "eligible_consensus_candles": {
            "LONG": eligible_consensus["LONG"],
            "SHORT": eligible_consensus["SHORT"],
        },
        "strategy_assessments": {
            name: {key: counts[key] for key in (
                "LONG", "SHORT", "NEUTRAL", "qualified_LONG", "qualified_SHORT"
            )}
            for name, counts in sorted(strategy_counts.items())
        },
        "minimum_signal_score": minimum_signal_score,
        "orders_authorized": False,
    }


def diagnose_strategy_threshold_sweep(
    symbol: str,
    candles: Iterable[Candle],
    *,
    signal_start_index: int,
    signal_end_index_exclusive: int,
    thresholds: Iterable[float] = (0.0, 20.0, 40.0, 60.0, 80.0),
    research_config: ResearchConfig | None = None,
) -> dict[str, object]:
    """Measure candidate frequency across score thresholds without tuning or trading.

    This is an activity diagnostic, not a backtest: it reports how often two or more
    directional strategy assessments agree at each threshold. It must not be used as
    evidence of profitability or to choose thresholds using an untouched holdout.
    """
    config = research_config or ResearchConfig()
    rows = list(candles)
    score_thresholds = tuple(float(value) for value in thresholds)
    if not symbol or not rows:
        raise ValueError("symbol and candle data are required")
    if not score_thresholds:
        raise ValueError("at least one score threshold is required")
    if any(not math.isfinite(value) or not 0 <= value <= 100 for value in score_thresholds):
        raise ValueError("score thresholds must be finite values between 0 and 100")
    if len(set(score_thresholds)) != len(score_thresholds):
        raise ValueError("score thresholds must be unique")
    if signal_start_index < config.min_candles - 1:
        raise ValueError("signal range must include enough prior candles for strategy warmup")
    if signal_end_index_exclusive > len(rows):
        raise ValueError("signal range ends beyond available candle data")
    if signal_start_index < 0 or signal_end_index_exclusive <= signal_start_index:
        raise ValueError("signal range must be a non-empty valid index range")

    counts = {
        threshold: {
            "decision_counts": Counter({"NO_TRADE": 0, "REVIEW_LONG": 0, "REVIEW_SHORT": 0}),
            "consensus_candles": Counter({"LONG": 0, "SHORT": 0}),
        }
        for threshold in score_thresholds
    }
    for index in range(signal_start_index, signal_end_index_exclusive):
        window_start = max(0, index - config.min_candles + 1)
        window = rows[window_start:index + 1]
        if len(window) < config.min_candles:
            raise ValueError("signal range does not provide complete strategy warmup")
        result = research_strategies(symbol, window, now=window[-1].timestamp, config=config)
        for threshold in score_thresholds:
            qualified = Counter(
                assessment.direction
                for assessment in result.assessments
                if assessment.direction in ("LONG", "SHORT")
                and assessment.confidence_score >= threshold
            )
            long_count, short_count = qualified["LONG"], qualified["SHORT"]
            if long_count >= 2 and long_count > short_count:
                decision = "REVIEW_LONG"
                counts[threshold]["consensus_candles"]["LONG"] += 1
            elif short_count >= 2 and short_count > long_count:
                decision = "REVIEW_SHORT"
                counts[threshold]["consensus_candles"]["SHORT"] += 1
            else:
                decision = "NO_TRADE"
            counts[threshold]["decision_counts"][decision] += 1

    return {
        "signal_start_index": signal_start_index,
        "signal_end_index_exclusive": signal_end_index_exclusive,
        "evaluated_signal_candles": signal_end_index_exclusive - signal_start_index,
        "thresholds": [
            {
                "minimum_score": threshold,
                "decision_counts": {
                    decision: counts[threshold]["decision_counts"][decision]
                    for decision in ("NO_TRADE", "REVIEW_LONG", "REVIEW_SHORT")
                },
                "consensus_candles": {
                    direction: counts[threshold]["consensus_candles"][direction]
                    for direction in ("LONG", "SHORT")
                },
                "orders_authorized": False,
            }
            for threshold in score_thresholds
        ],
        "orders_authorized": False,
        "interpretation": "Signal frequency only; not performance evidence or a threshold recommendation.",
    }
