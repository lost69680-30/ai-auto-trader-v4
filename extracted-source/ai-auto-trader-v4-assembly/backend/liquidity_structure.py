"""Rule-based liquidity and market-structure research; never authorizes orders.

This module detects candidate patterns from caller-supplied, already-normalized
OHLCV and funding data. It does not fetch exchange data, predict profitability,
or execute trades. Scores are screening heuristics and must be calibrated with
walk-forward testing and realistic transaction costs before any production use.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Iterable


@dataclass(frozen=True, slots=True)
class Candle:
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class MarketStructureInput:
    symbol: str
    candles: tuple[Candle, ...]
    timestamp: datetime
    reference_level: float
    previous_close: float
    current_funding_rate: float
    previous_funding_rate: float
    volume_baseline: float
    estimated_round_trip_cost_pct: float


@dataclass(frozen=True, slots=True)
class LiquidityConfig:
    lookback_candles: int = 4
    breakout_volume_multiple: float = 1.25
    funding_step_threshold: float = 0.0003
    high_funding_threshold: float = 0.0005
    minimum_score: float = 25.0
    max_data_age_seconds: int = 120
    max_future_skew_seconds: int = 2
    max_round_trip_cost_pct: float = 1.0

    def __post_init__(self) -> None:
        if self.lookback_candles < 2:
            raise ValueError("lookback_candles must be at least 2")
        for name in ("breakout_volume_multiple", "funding_step_threshold",
                     "high_funding_threshold", "max_round_trip_cost_pct"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not math.isfinite(self.minimum_score) or not 0 <= self.minimum_score <= 100:
            raise ValueError("minimum_score must be between 0 and 100")
        if self.max_data_age_seconds <= 0 or self.max_future_skew_seconds < 0:
            raise ValueError("data-age limit must be positive and future skew non-negative")


@dataclass(frozen=True, slots=True)
class RankedOpportunity:
    symbol: str
    direction: str
    score: float
    rank: int
    patterns: tuple[str, ...]
    evidence: tuple[str, ...]
    timestamp: datetime
    estimated_round_trip_cost_pct: float
    status: str = "REVIEW"
    orders_authorized: bool = False


def _valid_input(item: MarketStructureInput, now: datetime, config: LiquidityConfig) -> bool:
    if not item.symbol.strip() or len(item.candles) < config.lookback_candles + 1:
        return False
    if item.timestamp.tzinfo is None or item.timestamp.utcoffset() is None:
        return False
    age = (now.astimezone(timezone.utc) - item.timestamp.astimezone(timezone.utc)).total_seconds()
    if age > config.max_data_age_seconds or age < -config.max_future_skew_seconds:
        return False
    values = (item.reference_level, item.previous_close, item.current_funding_rate,
              item.previous_funding_rate, item.volume_baseline,
              item.estimated_round_trip_cost_pct)
    if not all(math.isfinite(value) for value in values):
        return False
    if item.reference_level <= 0 or item.previous_close <= 0 or item.volume_baseline <= 0:
        return False
    if item.estimated_round_trip_cost_pct < 0 or item.estimated_round_trip_cost_pct > config.max_round_trip_cost_pct:
        return False
    for candle in item.candles:
        nums = (candle.open, candle.high, candle.low, candle.close, candle.volume)
        if not all(math.isfinite(value) for value in nums):
            return False
        if min(candle.open, candle.high, candle.low, candle.close) <= 0 or candle.volume < 0:
            return False
        if candle.high < max(candle.open, candle.close, candle.low) or candle.low > min(candle.open, candle.close, candle.high):
            return False
    return True


def _analyse(item: MarketStructureInput, config: LiquidityConfig) -> tuple[str, float, tuple[str, ...], tuple[str, ...]] | None:
    candles = item.candles
    current = candles[-1]
    history = candles[-(config.lookback_candles + 1):-1]
    prior_high = max(c.high for c in history)
    prior_low = min(c.low for c in history)
    patterns: list[str] = []
    evidence: list[str] = []
    long_points = 0.0
    short_points = 0.0

    # A sweep pokes through a prior extreme but closes back inside that range.
    if current.high > prior_high and current.close < prior_high:
        patterns.append("LIQUIDITY_SWEEP_HIGH")
        evidence.append(f"high {current.high:.8g} swept prior high {prior_high:.8g} and closed back below")
        short_points += 34
    if current.low < prior_low and current.close > prior_low:
        patterns.append("LIQUIDITY_SWEEP_LOW")
        evidence.append(f"low {current.low:.8g} swept prior low {prior_low:.8g} and closed back above")
        long_points += 34

    # Breakout requires a close beyond the prior range plus volume confirmation.
    volume_multiple = current.volume / item.volume_baseline
    if current.close > prior_high and volume_multiple >= config.breakout_volume_multiple:
        patterns.append("BREAKOUT_UP")
        evidence.append(f"close broke prior high with {volume_multiple:.2f}x baseline volume")
        long_points += 32 + min(8.0, (volume_multiple - config.breakout_volume_multiple) * 8.0)
    if current.close < prior_low and volume_multiple >= config.breakout_volume_multiple:
        patterns.append("BREAKOUT_DOWN")
        evidence.append(f"close broke prior low with {volume_multiple:.2f}x baseline volume")
        short_points += 32 + min(8.0, (volume_multiple - config.breakout_volume_multiple) * 8.0)

    # Reclaim: price crosses a supplied reference and the candle tests then closes beyond it.
    if item.previous_close < item.reference_level < current.close and current.low <= item.reference_level:
        patterns.append("RECLAIM_UP")
        evidence.append("price crossed and reclaimed reference level from below")
        long_points += 24
    if item.previous_close > item.reference_level > current.close and current.high >= item.reference_level:
        patterns.append("RECLAIM_DOWN")
        evidence.append("price crossed and reclaimed reference level from above")
        short_points += 24

    # Funding is contextual evidence only, never sufficient by itself for an actionable setup.
    funding_delta = item.current_funding_rate - item.previous_funding_rate
    if funding_delta >= config.funding_step_threshold and item.current_funding_rate >= config.high_funding_threshold:
        patterns.append("FUNDING_STEP_UP")
        evidence.append(f"funding rose by {funding_delta:.6g}; elevated positive funding may indicate crowded longs")
        short_points += 10
    elif funding_delta <= -config.funding_step_threshold and item.current_funding_rate <= -config.high_funding_threshold:
        patterns.append("FUNDING_STEP_DOWN")
        evidence.append(f"funding fell by {funding_delta:.6g}; elevated negative funding may indicate crowded shorts")
        long_points += 10

    if not patterns:
        return None
    if long_points > short_points:
        direction, raw_score = "LONG", long_points
    elif short_points > long_points:
        direction, raw_score = "SHORT", short_points
    else:
        direction, raw_score = "NEUTRAL", max(long_points, short_points)
    # Penalize high friction. This is a ranking heuristic, not expected return.
    cost_penalty = min(30.0, item.estimated_round_trip_cost_pct * 15.0)
    score = max(0.0, min(100.0, raw_score - cost_penalty))
    has_technical_pattern = any(pattern not in {"FUNDING_STEP_UP", "FUNDING_STEP_DOWN"} for pattern in patterns)
    if has_technical_pattern and score < config.minimum_score:
        return None
    return direction, score, tuple(patterns), tuple(evidence)


def rank_opportunities(
    inputs: Iterable[MarketStructureInput],
    *,
    now: datetime | None = None,
    config: LiquidityConfig | None = None,
) -> tuple[RankedOpportunity, ...]:
    """Detect and rank market-structure candidates, without placing or authorizing orders.

    Duplicate symbols fail closed for the whole batch because competing snapshots
    for the same symbol would make the ranking ambiguous. Invalid items are skipped.
    """
    config = config or LiquidityConfig()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    batch = tuple(inputs)
    symbols = [item.symbol.strip().upper() for item in batch]
    if len(symbols) != len(set(symbols)):
        return ()
    found: list[RankedOpportunity] = []
    for item in batch:
        if not _valid_input(item, now, config):
            continue
        analysis = _analyse(item, config)
        if analysis is None:
            continue
        direction, score, patterns, evidence = analysis
        # Funding-only observations remain context, not standalone trade signals.
        technical_patterns = set(patterns) - {"FUNDING_STEP_UP", "FUNDING_STEP_DOWN"}
        if not technical_patterns:
            direction = "NEUTRAL"
            evidence = evidence + ("funding observation alone is not an actionable setup",)
        found.append(RankedOpportunity(
            symbol=item.symbol.strip().upper(), direction=direction, score=round(score, 2),
            rank=0, patterns=patterns, evidence=evidence, timestamp=item.timestamp,
            estimated_round_trip_cost_pct=item.estimated_round_trip_cost_pct,
            status="REVIEW" if technical_patterns else "MONITOR_ONLY",
            orders_authorized=False,
        ))
    found.sort(key=lambda result: (-result.score, result.symbol, result.direction, result.patterns))
    return tuple(
        RankedOpportunity(
            symbol=result.symbol, direction=result.direction, score=result.score,
            rank=index, patterns=result.patterns, evidence=result.evidence,
            timestamp=result.timestamp, estimated_round_trip_cost_pct=result.estimated_round_trip_cost_pct,
            status=result.status, orders_authorized=False,
        )
        for index, result in enumerate(found, start=1)
    )
