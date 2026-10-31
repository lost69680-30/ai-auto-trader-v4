"""Adaptive, fail-closed strategy ranking for research and paper proposals.

This module ranks strategy fit; it does not create orders, size positions, modify
risk limits, or authorize execution. All outputs retain orders_authorized=False.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

from backend.market_assessment import MarketAssessment, TimeframeAssessment


_EXECUTABLE_SPECS = {
    "SCALPING": {
        "timeframes": ("1m", "5m", "15m", "1h"),
        "regimes": ("TRENDING", "HIGH_VOLATILITY"),
        "max_spread_bps": 8.0,
        "max_cost_bps": 18.0,
        "tie_priority": 4,
    },
    "DAY_TRADING": {
        "timeframes": ("5m", "15m", "1h", "4h"),
        "regimes": ("TRENDING", "RANGING", "HIGH_VOLATILITY"),
        "max_spread_bps": 15.0,
        "max_cost_bps": 30.0,
        "tie_priority": 2,
    },
    "SWING_TRADING": {
        "timeframes": ("1h", "4h", "1d", "1w"),
        "regimes": ("TRENDING",),
        "max_spread_bps": 25.0,
        "max_cost_bps": 40.0,
        "tie_priority": 1,
    },
    "POSITION_TRADING": {
        "timeframes": ("1d", "1w", "1M"),
        "regimes": ("TRENDING",),
        "max_spread_bps": 40.0,
        "max_cost_bps": 60.0,
        "tie_priority": 0,
    },
}
_LONG_TERM_TIMEFRAMES = ("1w", "1M", "1Y")
_DEFAULT_ENABLED = tuple(_EXECUTABLE_SPECS)


@dataclass(frozen=True, slots=True)
class AdaptiveStrategyConfig:
    min_quote_volume_24h: float = 1_000_000.0
    max_spread_bps: float = 40.0
    max_round_trip_cost_bps: float = 60.0
    minimum_fit_score: float = 55.0

    def __post_init__(self) -> None:
        values = (
            self.min_quote_volume_24h, self.max_spread_bps,
            self.max_round_trip_cost_bps, self.minimum_fit_score,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("adaptive strategy thresholds must be finite")
        if self.min_quote_volume_24h < 0 or self.max_spread_bps < 0 or self.max_round_trip_cost_bps < 0:
            raise ValueError("liquidity and cost thresholds must be non-negative")
        if not 0 <= self.minimum_fit_score <= 100:
            raise ValueError("minimum_fit_score must be between 0 and 100")


@dataclass(frozen=True, slots=True)
class StrategyRank:
    strategy: str
    eligible: bool
    fit_score: float | None
    direction: str
    reasons: tuple[str, ...]
    eligible_for_risk_review: bool
    orders_authorized: bool = False


@dataclass(frozen=True, slots=True)
class StrategySelection:
    decision: str
    selected_strategy: str | None
    direction: str
    fit_score: float | None
    execution_eligible: bool
    no_trade_reason: str | None
    ranking: tuple[StrategyRank, ...]
    orders_authorized: bool = False


def _timeframe_index(assessment: MarketAssessment) -> dict[str, TimeframeAssessment]:
    return {row.timeframe: row for row in assessment.timeframes}


def _validate_metric(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return float(value)


def _aligned_direction(
    by_timeframe: dict[str, TimeframeAssessment], required: tuple[str, ...],
) -> tuple[str | None, list[str]]:
    reasons: list[str] = []
    rows: list[TimeframeAssessment] = []
    for timeframe in required:
        row = by_timeframe.get(timeframe)
        if row is None or row.status != "HEALTHY" or row.trend_score is None:
            reasons.append(f"{timeframe} timeframe is missing, unhealthy, or insufficient")
        else:
            rows.append(row)
    if reasons:
        return None, reasons
    directions = {row.direction for row in rows}
    if directions == {"BULLISH"}:
        return "LONG", []
    if directions == {"BEARISH"}:
        return "SHORT", []
    return None, ["required timeframes do not agree on a single direction"]


def _fit_score(
    by_timeframe: dict[str, TimeframeAssessment],
    required: tuple[str, ...],
    direction: str,
    regime: str,
    spread_bps: float,
    cost_bps: float,
) -> float:
    strengths = []
    for timeframe in required:
        score = by_timeframe[timeframe].trend_score
        directional_score = score if direction == "LONG" else 100.0 - score
        strengths.append(max(0.0, min(100.0, (directional_score - 50.0) * 2.0)))
    evidence_strength = sum(strengths) / len(strengths)
    regime_bonus = 8.0 if regime == "TRENDING" else (4.0 if regime == "HIGH_VOLATILITY" else 0.0)
    cost_penalty = min(12.0, spread_bps * 0.25 + cost_bps * 0.10)
    return max(0.0, min(100.0, 50.0 + evidence_strength * 0.35 + regime_bonus - cost_penalty))


def select_strategy(
    assessment: MarketAssessment,
    *,
    market_type: str,
    spread_bps: float | None = None,
    quote_volume_24h: float | None = None,
    estimated_round_trip_cost_bps: float | None = None,
    enabled_strategies: Iterable[str] | None = None,
    config: AdaptiveStrategyConfig | None = None,
) -> StrategySelection:
    """Choose one best-fit strategy, analysis-only, or NO_TRADE.

    Executable candidates require fresh directional evidence on every required
    timeframe plus explicit liquidity, spread and round-trip cost measurements.
    Spot markets cannot produce SHORT proposals. Fit scores are ranking heuristics,
    not calibrated confidence or profitability estimates.
    """
    if not isinstance(assessment, MarketAssessment) or assessment.orders_authorized is not False:
        raise ValueError("a safe MarketAssessment with orders_authorized=False is required")
    if market_type not in {"SPOT", "FUTURES"}:
        raise ValueError("market_type must be SPOT or FUTURES")
    cfg = config or AdaptiveStrategyConfig()
    spread = _validate_metric(spread_bps, "spread_bps")
    volume = _validate_metric(quote_volume_24h, "quote_volume_24h")
    costs = _validate_metric(estimated_round_trip_cost_bps, "estimated_round_trip_cost_bps")

    enabled = set(_DEFAULT_ENABLED if enabled_strategies is None else enabled_strategies)
    unknown = enabled.difference(set(_EXECUTABLE_SPECS))
    if unknown:
        raise ValueError(f"unknown executable strategies: {', '.join(sorted(map(str, unknown)))}")

    by_timeframe = _timeframe_index(assessment)
    ranking: list[StrategyRank] = []
    for name, spec in _EXECUTABLE_SPECS.items():
        if name not in enabled:
            ranking.append(StrategyRank(name, False, None, "NONE", ("strategy is disabled",), False, False))
            continue
        direction, reasons = _aligned_direction(by_timeframe, spec["timeframes"])
        if direction is not None and market_type == "SPOT" and direction == "SHORT":
            reasons.append("Spot mode cannot open a short position")
        if assessment.market_regime not in spec["regimes"]:
            reasons.append(f"market regime {assessment.market_regime} is not suitable for this strategy")
        if spread is None or volume is None or costs is None:
            reasons.append("liquidity, spread, or transaction-cost evidence is missing")
        else:
            if volume < cfg.min_quote_volume_24h:
                reasons.append("24-hour quote volume is below the configured liquidity floor")
            if spread > min(cfg.max_spread_bps, spec["max_spread_bps"]):
                reasons.append("spread exceeds the strategy's allowed cost boundary")
            if costs > min(cfg.max_round_trip_cost_bps, spec["max_cost_bps"]):
                reasons.append("estimated round-trip cost exceeds the strategy's allowed boundary")

        eligible = not reasons and direction is not None
        fit = None
        if eligible:
            fit = _fit_score(
                by_timeframe, spec["timeframes"], direction, assessment.market_regime,
                spread, costs,
            )
            if fit < cfg.minimum_fit_score:
                eligible = False
                reasons.append("strategy fit score is below the configured research threshold")
        ranking.append(StrategyRank(
            name, eligible, fit, direction or "NONE", tuple(reasons),
            eligible, False,
        ))

    eligible_rows = [row for row in ranking if row.eligible and row.eligible_for_risk_review]
    if eligible_rows:
        selected = sorted(
            eligible_rows,
            key=lambda row: (
                -float(row.fit_score),
                _EXECUTABLE_SPECS[row.strategy]["tie_priority"],
            ),
        )[0]
        return StrategySelection(
            "SELECTED", selected.strategy, selected.direction, selected.fit_score,
            True, None, tuple(ranking), False,
        )

    long_term_directions = []
    for timeframe in _LONG_TERM_TIMEFRAMES:
        row = by_timeframe.get(timeframe)
        if row is not None and row.status == "HEALTHY" and row.direction in {"BULLISH", "BEARISH"}:
            long_term_directions.append(row.direction)
    if len(long_term_directions) >= 2 and len(set(long_term_directions)) == 1:
        direction = "LONG" if long_term_directions[0] == "BULLISH" else "SHORT"
        long_term_score = 50.0 + sum(
            abs((by_timeframe[name].trend_score or 50.0) - 50.0)
            for name in _LONG_TERM_TIMEFRAMES
            if name in by_timeframe and by_timeframe[name].status == "HEALTHY"
        ) / len(long_term_directions)
        ranking.append(StrategyRank(
            "LONG_TERM_ANALYSIS", True, min(100.0, long_term_score), direction,
            ("long-term context is available; this is analysis-only",), False, False,
        ))
        return StrategySelection(
            "ANALYSIS_ONLY", "LONG_TERM_ANALYSIS", direction,
            min(100.0, long_term_score), False,
            "no executable strategy passed all evidence and cost checks",
            tuple(ranking), False,
        )

    ranking.append(StrategyRank(
        "LONG_TERM_ANALYSIS", False, None, "NONE",
        ("at least two aligned healthy weekly, monthly, or yearly timeframes are required",),
        False, False,
    ))
    return StrategySelection(
        "NO_TRADE", None, "NONE", None, False,
        "no enabled strategy passed timeframe, market-regime, liquidity and cost checks",
        tuple(ranking), False,
    )
