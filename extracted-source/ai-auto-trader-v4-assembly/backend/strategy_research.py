"""Deterministic, research-only strategy comparison for OHLCV candles.

This module intentionally has no exchange client, account access, or order interface.
Its scores are screening heuristics, not calibrated probabilities or profit forecasts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from statistics import fmean, pstdev
from typing import Iterable


@dataclass(frozen=True, slots=True)
class Candle:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class ResearchConfig:
    min_candles: int = 30
    max_candle_age_seconds: int = 180
    max_future_skew_seconds: int = 2
    trend_fast_period: int = 5
    trend_slow_period: int = 20
    momentum_period: int = 8
    breakout_period: int = 20
    mean_reversion_period: int = 20
    minimum_return_pct: float = 0.25
    breakout_buffer_pct: float = 0.05
    mean_reversion_z_threshold: float = 1.8
    minimum_directional_score: float = 60.0

    def __post_init__(self) -> None:
        if self.min_candles < 3:
            raise ValueError("min_candles must be at least 3")
        if self.max_candle_age_seconds <= 0 or self.max_future_skew_seconds < 0:
            raise ValueError("candle time limits are invalid")
        periods = (self.trend_fast_period, self.trend_slow_period, self.momentum_period,
                   self.breakout_period, self.mean_reversion_period)
        if any(period < 2 for period in periods):
            raise ValueError("strategy periods must be at least 2")
        if self.trend_fast_period >= self.trend_slow_period:
            raise ValueError("trend_fast_period must be smaller than trend_slow_period")
        if self.min_candles < max(self.trend_slow_period, self.breakout_period,
                                  self.mean_reversion_period, self.momentum_period + 1):
            raise ValueError("min_candles must cover all strategy lookback periods")
        thresholds = (self.minimum_return_pct, self.breakout_buffer_pct,
                      self.mean_reversion_z_threshold, self.minimum_directional_score)
        if not all(math.isfinite(v) for v in thresholds):
            raise ValueError("strategy thresholds must be finite")
        if self.minimum_return_pct < 0 or self.breakout_buffer_pct < 0:
            raise ValueError("return and breakout thresholds must be non-negative")
        if self.mean_reversion_z_threshold <= 0 or not 0 <= self.minimum_directional_score <= 100:
            raise ValueError("strategy score thresholds are invalid")


@dataclass(frozen=True, slots=True)
class StrategyAssessment:
    strategy: str
    direction: str
    confidence_score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResearchResult:
    symbol: str
    decision: str
    assessments: tuple[StrategyAssessment, ...]
    reasons: tuple[str, ...]
    orders_authorized: bool = False


def _ema(values: list[float], period: int) -> float:
    alpha = 2.0 / (period + 1.0)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def _neutral(strategy: str, reason: str) -> StrategyAssessment:
    return StrategyAssessment(strategy, "NEUTRAL", 0.0, (reason,))


def _validate_candles(symbol: str, candles: list[Candle], now: datetime,
                      config: ResearchConfig) -> str | None:
    if not symbol or not candles:
        return "symbol or candle history is missing"
    if len(candles) < config.min_candles:
        return f"insufficient history: need at least {config.min_candles} candles"
    previous: datetime | None = None
    for candle in candles:
        if candle.symbol != symbol:
            return "mixed candle symbols; rejected fail-closed"
        if candle.timestamp.tzinfo is None or candle.timestamp.utcoffset() is None:
            return "candle timestamp is not timezone-aware"
        timestamp = candle.timestamp.astimezone(timezone.utc)
        if timestamp > now + timedelta(seconds=config.max_future_skew_seconds):
            return "candle timestamp is in the future"
        if previous is not None and timestamp <= previous:
            return "candle timestamps must be strictly increasing and unique"
        previous = timestamp
        values = (candle.open, candle.high, candle.low, candle.close, candle.volume)
        if not all(math.isfinite(v) for v in values):
            return "candle contains non-finite values"
        if min(candle.open, candle.high, candle.low, candle.close) <= 0 or candle.volume < 0:
            return "candle prices must be positive and volume non-negative"
        if candle.high < max(candle.open, candle.close, candle.low):
            return "candle high is inconsistent with OHLC values"
        if candle.low > min(candle.open, candle.close, candle.high):
            return "candle low is inconsistent with OHLC values"
    age = (now - candles[-1].timestamp.astimezone(timezone.utc)).total_seconds()
    if age < -config.max_future_skew_seconds:
        return "latest candle timestamp is in the future"
    if age > config.max_candle_age_seconds:
        return "latest candle is stale"
    return None


def research_strategies(
    symbol: str,
    candles: Iterable[Candle],
    *,
    now: datetime,
    config: ResearchConfig | None = None,
) -> ResearchResult:
    """Compare four simple strategy families and emit review-only research results."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    config = config or ResearchConfig()
    now = now.astimezone(timezone.utc)
    rows = list(candles)
    error = _validate_candles(symbol, rows, now, config)
    if error:
        names = ("trend_following", "momentum", "breakout", "mean_reversion")
        assessments = tuple(_neutral(name, error) for name in names)
        return ResearchResult(symbol, "NO_TRADE", assessments, (error,), False)

    closes = [row.close for row in rows]
    last = closes[-1]
    assessments: list[StrategyAssessment] = []

    # Trend following: compare short/long EMAs and recent slope.
    fast = _ema(closes[-config.trend_fast_period:], config.trend_fast_period)
    slow = _ema(closes[-config.trend_slow_period:], config.trend_slow_period)
    trend_return = (last / closes[-config.trend_slow_period] - 1.0) * 100.0
    trend_gap = (fast / slow - 1.0) * 100.0
    if trend_gap > 0 and trend_return >= config.minimum_return_pct:
        direction = "LONG"
    elif trend_gap < 0 and trend_return <= -config.minimum_return_pct:
        direction = "SHORT"
    else:
        direction = "NEUTRAL"
    trend_score = min(100.0, abs(trend_gap) * 20 + abs(trend_return) * 3) if direction != "NEUTRAL" else 0.0
    assessments.append(StrategyAssessment("trend_following", direction, round(trend_score, 2),
        (f"EMA gap {trend_gap:.3f}%", f"lookback return {trend_return:.3f}%")))

    # Momentum: a thresholded return over the configured lookback.
    momentum_return = (last / closes[-config.momentum_period - 1] - 1.0) * 100.0
    momentum_direction = "LONG" if momentum_return >= config.minimum_return_pct else (
        "SHORT" if momentum_return <= -config.minimum_return_pct else "NEUTRAL")
    momentum_score = min(100.0, abs(momentum_return) * 12) if momentum_direction != "NEUTRAL" else 0.0
    assessments.append(StrategyAssessment("momentum", momentum_direction, round(momentum_score, 2),
        (f"{config.momentum_period}-interval return {momentum_return:.3f}%",)))

    # Breakout: current close must exceed the previous window's high/low by a buffer.
    window = rows[-config.breakout_period - 1:-1]
    prior_high = max(row.high for row in window)
    prior_low = min(row.low for row in window)
    buffer = config.breakout_buffer_pct / 100.0
    if last > prior_high * (1.0 + buffer):
        breakout_direction = "LONG"
        breakout_distance = (last / prior_high - 1.0) * 100.0
    elif last < prior_low * (1.0 - buffer):
        breakout_direction = "SHORT"
        breakout_distance = (prior_low / last - 1.0) * 100.0
    else:
        breakout_direction = "NEUTRAL"
        breakout_distance = 0.0
    breakout_score = min(100.0, breakout_distance * 40) if breakout_direction != "NEUTRAL" else 0.0
    assessments.append(StrategyAssessment("breakout", breakout_direction, round(breakout_score, 2),
        (f"prior-window high {prior_high:.6g}", f"prior-window low {prior_low:.6g}")))

    # Mean reversion: flag an unusually distant close, pointing back toward the mean.
    sample = closes[-config.mean_reversion_period:]
    mean = fmean(sample)
    deviation = pstdev(sample)
    z_score = (last - mean) / deviation if deviation > 0 else 0.0
    if z_score >= config.mean_reversion_z_threshold:
        mr_direction = "SHORT"
    elif z_score <= -config.mean_reversion_z_threshold:
        mr_direction = "LONG"
    else:
        mr_direction = "NEUTRAL"
    mr_score = min(100.0, abs(z_score) * 25) if mr_direction != "NEUTRAL" else 0.0
    assessments.append(StrategyAssessment("mean_reversion", mr_direction, round(mr_score, 2),
        (f"close z-score {z_score:.3f}",)))

    assessments.sort(key=lambda item: (-item.confidence_score, item.strategy))
    directional = [item for item in assessments if item.direction != "NEUTRAL"
                   and item.confidence_score >= config.minimum_directional_score]
    longs = sum(item.direction == "LONG" for item in directional)
    shorts = sum(item.direction == "SHORT" for item in directional)
    if longs >= 2 and longs > shorts:
        decision = "REVIEW_LONG"
        reasons = ("multiple strategy families align; research review only",)
    elif shorts >= 2 and shorts > longs:
        decision = "REVIEW_SHORT"
        reasons = ("multiple strategy families align; research review only",)
    else:
        decision = "NO_TRADE"
        reasons = ("no sufficiently strong multi-strategy consensus",)
    return ResearchResult(symbol, decision, tuple(assessments), reasons, False)
