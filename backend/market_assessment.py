"""Hierarchical multi-timeframe market assessment.

This is a deterministic research layer. Scores are heuristic evidence summaries,
not calibrated probabilities or forecasts. It has no account, order, or risk-setting
interface and always reports orders_authorized=False.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from statistics import fmean
from typing import Iterable, Mapping

from backend.market_data_adapter import MarketCandle


TIMEFRAME_ORDER = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M", "1Y")
LONG_TERM_TIMEFRAMES = ("1Y", "1M", "1w")
MEDIUM_TERM_TIMEFRAMES = ("1d", "4h", "1h")
SHORT_TERM_TIMEFRAMES = ("30m", "15m", "5m", "1m")

_FIXED_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800,
}
_MINIMUM_HISTORY = {
    "1m": 30, "5m": 30, "15m": 30, "30m": 30,
    "1h": 30, "4h": 30, "1d": 20, "1w": 20, "1M": 12, "1Y": 2,
}
_GROUP_WEIGHTS = {
    "long": {"1Y": 0.40, "1M": 0.35, "1w": 0.25},
    "medium": {"1d": 0.40, "4h": 0.35, "1h": 0.25},
    "short": {"30m": 0.15, "15m": 0.25, "5m": 0.35, "1m": 0.25},
}


@dataclass(frozen=True, slots=True)
class MarketAssessmentConfig:
    max_future_skew_seconds: float = 2.0
    freshness_intervals: float = 2.0
    minimum_directional_score: float = 5.0
    high_volatility_pct: float = 2.5
    trending_efficiency_ratio: float = 0.35
    ranging_efficiency_ratio: float = 0.20

    def __post_init__(self) -> None:
        values = (
            self.max_future_skew_seconds, self.freshness_intervals,
            self.minimum_directional_score, self.high_volatility_pct,
            self.trending_efficiency_ratio, self.ranging_efficiency_ratio,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("market assessment thresholds must be finite")
        if self.max_future_skew_seconds < 0 or self.freshness_intervals <= 0:
            raise ValueError("market timestamp thresholds are invalid")
        if not 0 < self.minimum_directional_score < 50:
            raise ValueError("minimum_directional_score must be between 0 and 50")
        if self.high_volatility_pct <= 0:
            raise ValueError("high_volatility_pct must be positive")
        if not 0 <= self.ranging_efficiency_ratio < self.trending_efficiency_ratio <= 1:
            raise ValueError("efficiency-ratio thresholds are invalid")


@dataclass(frozen=True, slots=True)
class TimeframeAssessment:
    timeframe: str
    status: str
    direction: str
    trend_score: float | None
    momentum_pct: float | None
    volatility_pct: float | None
    efficiency_ratio: float | None
    relative_volume: float | None
    structure: str
    candlestick_pattern: str
    structure_event: str
    support: float | None
    resistance: float | None
    latest_close_time: datetime | None
    reasons: tuple[str, ...]
    orders_authorized: bool = False


@dataclass(frozen=True, slots=True)
class MarketAssessment:
    symbol: str
    assessed_at: datetime
    direction: str
    market_regime: str
    market_phase: str
    long_term_bias: str
    medium_term_bias: str
    short_term_bias: str
    conflicts: tuple[str, ...]
    timeframes: tuple[TimeframeAssessment, ...]
    orders_authorized: bool = False


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _next_open(opened: datetime, timeframe: str) -> datetime:
    if timeframe in _FIXED_SECONDS:
        return opened + timedelta(seconds=_FIXED_SECONDS[timeframe])
    if timeframe == "1M":
        if opened.month == 12:
            return datetime(opened.year + 1, 1, 1, tzinfo=timezone.utc)
        return datetime(opened.year, opened.month + 1, 1, tzinfo=timezone.utc)
    if timeframe == "1Y":
        return datetime(opened.year + 1, 1, 1, tzinfo=timezone.utc)
    raise ValueError("unsupported timeframe")


def _expected_close(opened: datetime, timeframe: str) -> datetime:
    return _next_open(opened, timeframe) - timedelta(milliseconds=1)


def _interval_freshness_seconds(timeframe: str) -> float:
    if timeframe in _FIXED_SECONDS:
        return max(90.0, _FIXED_SECONDS[timeframe] * 2.0)
    if timeframe == "1M":
        return 62.0 * 86400.0
    if timeframe == "1Y":
        return 2.0 * 366.0 * 86400.0
    raise ValueError("unsupported timeframe")


def _invalid_series_reason(
    symbol: str, timeframe: str, rows: tuple[MarketCandle, ...],
    now: datetime, config: MarketAssessmentConfig,
) -> str | None:
    if not rows:
        return "no candles supplied"
    if any(not isinstance(row, MarketCandle) for row in rows):
        return "candle payload has an invalid row type"
    if any(row.symbol != symbol for row in rows):
        return "candle symbol does not match requested symbol"
    if any(not row.is_closed for row in rows):
        return "an in-progress candle is present; current candle is not confirmed"
    opened: list[datetime] = []
    for row in rows:
        try:
            open_time = _utc(row.open_time, "candle open_time")
            close_time = _utc(row.close_time, "candle close_time")
        except ValueError as exc:
            return str(exc)
        if close_time <= open_time:
            return "candle close timestamp must follow open timestamp"
        if close_time > now + timedelta(seconds=config.max_future_skew_seconds):
            return "candle close timestamp is future-dated"
        values = (row.open, row.high, row.low, row.close, row.volume)
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) for value in values):
            return "candle contains a non-finite or non-numeric OHLCV value"
        if min(row.open, row.high, row.low, row.close) <= 0 or row.volume < 0:
            return "candle contains a non-positive price or negative volume"
        if row.high < max(row.open, row.close, row.low) or row.low > min(row.open, row.close, row.high):
            return "candle OHLC values are internally inconsistent"
        if close_time != _expected_close(open_time, timeframe):
            return "candle open/close boundaries do not match the requested timeframe"
        opened.append(open_time)
    if any(opened[index] <= opened[index - 1] for index in range(1, len(opened))):
        return "duplicate or out-of-order candle timestamps"
    for previous, current in zip(opened, opened[1:]):
        if current != _next_open(previous, timeframe):
            return "candle gap detected between consecutive bars"
    latest_close = _utc(rows[-1].close_time, "latest close_time")
    age = (now - latest_close).total_seconds()
    if age < -config.max_future_skew_seconds:
        return "latest candle is future-dated"
    if age > _interval_freshness_seconds(timeframe) * config.freshness_intervals:
        return "latest closed candle is stale"
    if len(rows) < _MINIMUM_HISTORY[timeframe]:
        return f"insufficient history: need {_MINIMUM_HISTORY[timeframe]} closed candles"
    return None


def _ema(values: list[float], period: int) -> float:
    alpha = 2.0 / (period + 1.0)
    result = values[0]
    for value in values[1:]:
        result += alpha * (value - result)
    return result


def _safe_latest_close(rows: tuple[MarketCandle, ...]) -> datetime | None:
    if not rows or not isinstance(rows[-1], MarketCandle):
        return None
    try:
        return _utc(rows[-1].close_time, "latest close_time")
    except ValueError:
        return None


def _measure(
    rows: tuple[MarketCandle, ...], minimum_directional_score: float,
) -> tuple[float, float, float, float, float, str, str, str, float, float]:
    recent = rows[-min(20, len(rows)):]
    closes = [float(row.close) for row in recent]
    momentum = (closes[-1] / closes[0] - 1.0) * 100.0 if closes[0] else 0.0
    mean_close = fmean(closes)
    ema = _ema(closes, min(20, len(closes)))
    close_vs_mean = (closes[-1] / mean_close - 1.0) * 100.0 if mean_close else 0.0
    close_vs_ema = (closes[-1] / ema - 1.0) * 100.0 if ema else 0.0
    trend_score = max(0.0, min(
        100.0, 50.0 + momentum * 0.35 + close_vs_mean * 2.0 + close_vs_ema * 2.0
    ))

    ranges: list[float] = []
    previous_close = None
    for row in rows[-15:]:
        true_range = row.high - row.low
        if previous_close is not None:
            true_range = max(true_range, abs(row.high - previous_close), abs(row.low - previous_close))
        ranges.append(float(true_range))
        previous_close = float(row.close)
    volatility = (fmean(ranges) / float(rows[-1].close)) * 100.0

    moves = [abs(closes[index] - closes[index - 1]) for index in range(1, len(closes))]
    efficiency = abs(closes[-1] - closes[0]) / sum(moves) if sum(moves) else 0.0
    prior_volumes = [float(row.volume) for row in rows[-21:-1]]
    baseline_volume = fmean(prior_volumes) if prior_volumes else 0.0
    relative_volume = float(rows[-1].volume) / baseline_volume if baseline_volume > 0 else 0.0

    if len(rows) >= 10:
        previous = rows[-10:-5]
        latest = rows[-5:]
        higher_highs = fmean([row.high for row in latest]) > fmean([row.high for row in previous])
        higher_lows = fmean([row.low for row in latest]) > fmean([row.low for row in previous])
        lower_highs = fmean([row.high for row in latest]) < fmean([row.high for row in previous])
        lower_lows = fmean([row.low for row in latest]) < fmean([row.low for row in previous])
        if higher_highs and higher_lows:
            structure = "HIGHER_HIGHS_HIGHER_LOWS"
        elif lower_highs and lower_lows:
            structure = "LOWER_HIGHS_LOWER_LOWS"
        else:
            structure = "MIXED_STRUCTURE"
    else:
        structure = "INSUFFICIENT_STRUCTURE"

    last = rows[-1]
    previous_candle = rows[-2]
    candle_range = max(float(last.high - last.low), 1e-12)
    body = abs(float(last.close - last.open))
    upper_wick = float(last.high - max(last.open, last.close))
    lower_wick = float(min(last.open, last.close) - last.low)
    if body / candle_range <= 0.10:
        pattern = "DOJI"
    elif (previous_candle.close < previous_candle.open and last.close > last.open
          and last.open <= previous_candle.close and last.close >= previous_candle.open):
        pattern = "BULLISH_ENGULFING"
    elif (previous_candle.close > previous_candle.open and last.close < last.open
          and last.open >= previous_candle.close and last.close <= previous_candle.open):
        pattern = "BEARISH_ENGULFING"
    elif body > 0 and lower_wick >= 2.0 * body and upper_wick <= body and last.close > last.open:
        pattern = "BULLISH_PIN_BAR"
    elif body > 0 and upper_wick >= 2.0 * body and lower_wick <= body and last.close < last.open:
        pattern = "BEARISH_PIN_BAR"
    else:
        pattern = "NONE"

    level_rows = rows[-21:-1]
    if len(level_rows) >= 10:
        prior_support = min(float(row.low) for row in level_rows)
        prior_resistance = max(float(row.high) for row in level_rows)
        if last.close > prior_resistance and relative_volume >= 1.2:
            structure_event = "BREAKOUT_UP"
        elif last.close < prior_support and relative_volume >= 1.2:
            structure_event = "BREAKOUT_DOWN"
        elif last.high > prior_resistance and last.close <= prior_resistance:
            structure_event = "FAILED_BREAKOUT_UP"
        elif last.low < prior_support and last.close >= prior_support:
            structure_event = "FAILED_BREAKOUT_DOWN"
        elif (trend_score >= 50.0 + minimum_directional_score
              and structure == "LOWER_HIGHS_LOWER_LOWS"
              and pattern in {"BULLISH_ENGULFING", "BULLISH_PIN_BAR"}):
            structure_event = "REVERSAL_ATTEMPT"
        elif (trend_score <= 50.0 - minimum_directional_score
              and structure == "HIGHER_HIGHS_HIGHER_LOWS"
              and pattern in {"BEARISH_ENGULFING", "BEARISH_PIN_BAR"}):
            structure_event = "REVERSAL_ATTEMPT"
        elif trend_score >= 50.0 + minimum_directional_score and structure == "HIGHER_HIGHS_HIGHER_LOWS":
            structure_event = "TREND_CONTINUATION"
        elif trend_score <= 50.0 - minimum_directional_score and structure == "LOWER_HIGHS_LOWER_LOWS":
            structure_event = "TREND_CONTINUATION"
        elif (trend_score >= 50.0 + minimum_directional_score and momentum < 0) or (
            trend_score <= 50.0 - minimum_directional_score and momentum > 0
        ):
            structure_event = "PULLBACK"
        elif efficiency <= 0.20:
            structure_event = "RANGE_CONSOLIDATION"
        else:
            structure_event = "UNCONFIRMED"
        support, resistance = prior_support, prior_resistance
    else:
        structure_event = "INSUFFICIENT_STRUCTURE"
        support = min(float(row.low) for row in recent)
        resistance = max(float(row.high) for row in recent)

    return (
        trend_score, momentum, volatility, efficiency, relative_volume,
        structure, pattern, structure_event, support, resistance,
    )


def _group_bias(
    assessments: Mapping[str, TimeframeAssessment], weights: Mapping[str, float],
    minimum_directional_score: float,
) -> str:
    usable = [(assessments[name], weight) for name, weight in weights.items()
              if name in assessments and assessments[name].status == "HEALTHY"
              and assessments[name].trend_score is not None]
    if not usable:
        return "INSUFFICIENT_DATA"
    total_weight = sum(weight for _, weight in usable)
    score = sum(item.trend_score * weight for item, weight in usable) / total_weight
    if score >= 50.0 + minimum_directional_score:
        return "BULLISH"
    if score <= 50.0 - minimum_directional_score:
        return "BEARISH"
    return "NEUTRAL"


def assess_market(
    symbol: str,
    candles_by_timeframe: Mapping[str, Iterable[MarketCandle]],
    *,
    now: datetime,
    config: MarketAssessmentConfig | None = None,
) -> MarketAssessment:
    """Assess available validated closed candles without majority-voting timeframes."""
    if not isinstance(symbol, str) or not symbol or symbol != symbol.strip().upper():
        raise ValueError("symbol must be a non-empty trimmed uppercase string")
    if not isinstance(candles_by_timeframe, Mapping):
        raise ValueError("candles_by_timeframe must be a mapping")
    stamp = _utc(now, "now")
    cfg = config or MarketAssessmentConfig()
    supplied = set(candles_by_timeframe)
    unknown = supplied.difference(TIMEFRAME_ORDER)
    if unknown:
        raise ValueError(f"unsupported timeframes: {', '.join(sorted(map(str, unknown)))}")

    by_name: dict[str, TimeframeAssessment] = {}
    for timeframe in TIMEFRAME_ORDER:
        raw = candles_by_timeframe.get(timeframe)
        if raw is None:
            by_name[timeframe] = TimeframeAssessment(
                timeframe, "MISSING", "INSUFFICIENT_DATA", None, None, None,
                None, None, "UNKNOWN", "UNKNOWN", "UNKNOWN", None, None, None,
                ("timeframe history was not supplied",), False,
            )
            continue
        rows = tuple(raw)
        reason = _invalid_series_reason(symbol, timeframe, rows, stamp, cfg)
        if reason:
            if "gap" in reason:
                status = "GAP_DETECTED"
            elif "stale" in reason or "future" in reason:
                status = "STALE"
            elif "in-progress" in reason:
                status = "IN_PROGRESS"
            elif "insufficient history" in reason:
                status = "INSUFFICIENT_HISTORY"
            else:
                status = "INVALID_DATA"
            by_name[timeframe] = TimeframeAssessment(
                timeframe, status, "INSUFFICIENT_DATA", None, None, None,
                None, None, "UNKNOWN", "UNKNOWN", "UNKNOWN", None, None,
                _safe_latest_close(rows),
                (reason,), False,
            )
            continue
        measured = _measure(rows, cfg.minimum_directional_score)
        score, momentum, volatility, efficiency, relative_volume, structure, pattern, structure_event, support, resistance = measured
        if score >= 50.0 + cfg.minimum_directional_score:
            direction = "BULLISH"
        elif score <= 50.0 - cfg.minimum_directional_score:
            direction = "BEARISH"
        else:
            direction = "NEUTRAL"
        by_name[timeframe] = TimeframeAssessment(
            timeframe, "HEALTHY", direction, score, momentum, volatility,
            efficiency, relative_volume, structure, pattern, structure_event,
            support, resistance, _utc(rows[-1].close_time, "latest close_time"), (), False,
        )

    long_bias = _group_bias(by_name, _GROUP_WEIGHTS["long"], cfg.minimum_directional_score)
    medium_bias = _group_bias(by_name, _GROUP_WEIGHTS["medium"], cfg.minimum_directional_score)
    short_bias = _group_bias(by_name, _GROUP_WEIGHTS["short"], cfg.minimum_directional_score)
    conflicts: list[str] = []
    if long_bias in {"BULLISH", "BEARISH"} and medium_bias in {"BULLISH", "BEARISH"} and long_bias != medium_bias:
        conflicts.append("long-term and medium-term directional evidence conflicts")
    if medium_bias in {"BULLISH", "BEARISH"} and short_bias in {"BULLISH", "BEARISH"} and medium_bias != short_bias:
        conflicts.append("short-term direction conflicts with medium-term structure")

    if long_bias in {"BULLISH", "BEARISH"} and medium_bias in {"BULLISH", "BEARISH"} and long_bias != medium_bias:
        direction = "MIXED"
    elif medium_bias in {"BULLISH", "BEARISH"}:
        direction = medium_bias
    elif medium_bias == "NEUTRAL":
        direction = "NEUTRAL"
    else:
        direction = "INSUFFICIENT_DATA"

    regime_rows = [by_name[name] for name in ("1d", "4h", "1h")
                   if by_name[name].status == "HEALTHY"]
    if any(row.volatility_pct is not None and row.volatility_pct >= cfg.high_volatility_pct
           for row in regime_rows):
        regime = "HIGH_VOLATILITY"
    elif regime_rows and fmean(row.efficiency_ratio for row in regime_rows) >= cfg.trending_efficiency_ratio:
        regime = "TRENDING"
    elif regime_rows and fmean(row.efficiency_ratio for row in regime_rows) <= cfg.ranging_efficiency_ratio:
        regime = "RANGING"
    else:
        regime = "UNCERTAIN"

    medium_events = [
        by_name[name].structure_event for name in ("1d", "4h", "1h")
        if by_name[name].status == "HEALTHY"
    ]
    if "BREAKOUT_UP" in medium_events:
        market_phase = "BREAKOUT_UP"
    elif "BREAKOUT_DOWN" in medium_events:
        market_phase = "BREAKOUT_DOWN"
    elif "FAILED_BREAKOUT_UP" in medium_events:
        market_phase = "FAILED_BREAKOUT_UP"
    elif "FAILED_BREAKOUT_DOWN" in medium_events:
        market_phase = "FAILED_BREAKOUT_DOWN"
    elif conflicts and "long-term and medium-term directional evidence conflicts" in conflicts:
        market_phase = "TIMEFRAME_CONFLICT"
    elif "REVERSAL_ATTEMPT" in medium_events:
        market_phase = "REVERSAL_ATTEMPT"
    elif "PULLBACK" in medium_events:
        market_phase = "PULLBACK"
    elif "TREND_CONTINUATION" in medium_events:
        market_phase = "TREND_CONTINUATION"
    elif regime == "RANGING":
        market_phase = "RANGE_CONSOLIDATION"
    else:
        market_phase = "UNCERTAIN"

    return MarketAssessment(
        symbol, stamp, direction, regime, market_phase, long_bias, medium_bias, short_bias,
        tuple(conflicts), tuple(by_name[name] for name in TIMEFRAME_ORDER), False,
    )
