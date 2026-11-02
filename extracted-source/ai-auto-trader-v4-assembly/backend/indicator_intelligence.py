"""Deterministic indicator intelligence over validated closed market candles.

Indicator values are descriptive research evidence, not calibrated probabilities,
trade instructions, risk approval, or order authorization.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from statistics import fmean, pstdev
from typing import Iterable

from backend.market_assessment import TIMEFRAME_ORDER, assess_market
from backend.market_data_adapter import MarketCandle


@dataclass(frozen=True, slots=True)
class IndicatorAssessment:
    symbol: str
    timeframe: str
    status: str
    latest_close_time: datetime | None
    ema_20: float | None
    ema_50: float | None
    rsi_14: float | None
    macd_line: float | None
    macd_signal: float | None
    macd_histogram: float | None
    bollinger_mid: float | None
    bollinger_upper: float | None
    bollinger_lower: float | None
    atr_14: float | None
    atr_percent: float | None
    vwap: float | None
    relative_volume_20: float | None
    trend: str
    momentum: str
    confluence: str
    reasons: tuple[str, ...]
    calibration_status: str = "NOT_CALIBRATED"
    orders_authorized: bool = False


def _ema(values: tuple[float, ...], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    current = fmean(values[:period])
    alpha = 2.0 / (period + 1.0)
    for value in values[period:]:
        current = alpha * value + (1.0 - alpha) * current
    return current if math.isfinite(current) else None


def _rsi(values: tuple[float, ...], period: int = 14) -> float | None:
    if len(values) < period + 1:
        return None
    changes = tuple(values[index] - values[index - 1] for index in range(1, len(values)))
    gains = fmean(max(change, 0.0) for change in changes[:period])
    losses = fmean(max(-change, 0.0) for change in changes[:period])
    for change in changes[period:]:
        gains = (gains * (period - 1) + max(change, 0.0)) / period
        losses = (losses * (period - 1) + max(-change, 0.0)) / period
    if losses == 0.0:
        return 100.0 if gains > 0.0 else 50.0
    result = 100.0 - 100.0 / (1.0 + gains / losses)
    return result if math.isfinite(result) else None


def _macd(values: tuple[float, ...]) -> tuple[float | None, float | None, float | None]:
    fast = _ema(values, 12)
    slow = _ema(values, 26)
    if fast is None or slow is None:
        return None, None, None
    line = fast - slow
    macd_history: list[float] = []
    # Match Solo's Android MACD history boundary: the signal EMA starts after
    # 26 prior closes, requiring 35 closes for nine MACD observations.
    for end in range(27, len(values) + 1):
        sample = values[:end]
        sample_fast = _ema(sample, 12)
        sample_slow = _ema(sample, 26)
        if sample_fast is not None and sample_slow is not None:
            macd_history.append(sample_fast - sample_slow)
    signal = _ema(tuple(macd_history), 9)
    histogram = line - signal if signal is not None else None
    return line, signal, histogram


def _atr(rows: tuple[MarketCandle, ...], period: int = 14) -> float | None:
    if len(rows) < period + 1:
        return None
    ranges: list[float] = []
    for previous, current in zip(rows, rows[1:]):
        ranges.append(max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        ))
    if len(ranges) < period:
        return None
    result = fmean(ranges[:period])
    for value in ranges[period:]:
        result = (result * (period - 1) + value) / period
    return result if math.isfinite(result) else None


def _empty(
    symbol: str,
    timeframe: str,
    status: str,
    latest_close_time: datetime | None,
    reasons: tuple[str, ...],
) -> IndicatorAssessment:
    return IndicatorAssessment(
        symbol=symbol,
        timeframe=timeframe,
        status=status,
        latest_close_time=latest_close_time,
        ema_20=None,
        ema_50=None,
        rsi_14=None,
        macd_line=None,
        macd_signal=None,
        macd_histogram=None,
        bollinger_mid=None,
        bollinger_upper=None,
        bollinger_lower=None,
        atr_14=None,
        atr_percent=None,
        vwap=None,
        relative_volume_20=None,
        trend="INSUFFICIENT_DATA",
        momentum="INSUFFICIENT_DATA",
        confluence="INSUFFICIENT_DATA",
        reasons=reasons,
    )


def assess_indicators(
    symbol: str,
    timeframe: str,
    candles: Iterable[MarketCandle],
    *,
    now: datetime,
) -> IndicatorAssessment:
    """Validate a candle series, then calculate transparent technical indicators."""
    if timeframe not in TIMEFRAME_ORDER:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    rows = tuple(candles)
    market = assess_market(symbol, {timeframe: rows}, now=now)
    timeframe_result = next(item for item in market.timeframes if item.timeframe == timeframe)
    if timeframe_result.status != "HEALTHY":
        return _empty(
            symbol, timeframe, timeframe_result.status,
            timeframe_result.latest_close_time, timeframe_result.reasons,
        )

    closes = tuple(float(row.close) for row in rows)
    reasons: list[str] = []
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    rsi14 = _rsi(closes, 14)
    macd_line, macd_signal, macd_histogram = _macd(closes)
    if ema20 is None:
        reasons.append("EMA20 unavailable: requires at least 20 closed candles")
    if ema50 is None:
        reasons.append("EMA50 unavailable: requires at least 50 closed candles")
    if rsi14 is None:
        reasons.append("RSI14 unavailable: requires at least 15 closed candles")
    if macd_line is None:
        reasons.append("MACD line unavailable: requires at least 26 closed candles")
    if macd_signal is None:
        reasons.append("MACD signal unavailable: requires at least 35 closed candles")

    bollinger_mid = bollinger_upper = bollinger_lower = None
    if len(closes) >= 20:
        last20 = closes[-20:]
        bollinger_mid = fmean(last20)
        deviation = pstdev(last20)
        bollinger_upper = bollinger_mid + 2.0 * deviation
        bollinger_lower = bollinger_mid - 2.0 * deviation
    else:
        reasons.append("Bollinger bands unavailable: requires at least 20 closed candles")

    atr14 = _atr(rows, 14)
    atr_percent = (100.0 * atr14 / closes[-1]) if atr14 is not None and closes[-1] > 0 else None
    if atr14 is None:
        reasons.append("ATR14 unavailable: requires at least 15 closed candles")

    total_volume = sum(float(row.volume) for row in rows)
    vwap = None
    if total_volume > 0:
        vwap = sum(
            ((float(row.high) + float(row.low) + float(row.close)) / 3.0) * float(row.volume)
            for row in rows
        ) / total_volume
    else:
        reasons.append("VWAP unavailable: total volume is zero")

    relative_volume = None
    if len(rows) >= 21:
        baseline = fmean(float(row.volume) for row in rows[-21:-1])
        if baseline > 0:
            relative_volume = float(rows[-1].volume) / baseline
        else:
            reasons.append("Relative volume unavailable: prior 20-candle volume baseline is zero")
    else:
        reasons.append("Relative volume unavailable: requires 21 closed candles")

    trend = "INSUFFICIENT_DATA"
    if ema20 is not None and ema50 is not None:
        if closes[-1] > ema20 > ema50:
            trend = "BULLISH"
        elif closes[-1] < ema20 < ema50:
            trend = "BEARISH"
        else:
            trend = "MIXED"

    momentum = "INSUFFICIENT_DATA"
    rsi_direction = None
    macd_direction = None
    if rsi14 is not None:
        rsi_direction = "BULLISH" if rsi14 >= 55.0 else "BEARISH" if rsi14 <= 45.0 else "NEUTRAL"
    if macd_line is not None and macd_signal is not None:
        macd_direction = "BULLISH" if macd_line > macd_signal else "BEARISH" if macd_line < macd_signal else "NEUTRAL"
    directional_momentum = [
        item for item in (rsi_direction, macd_direction)
        if item in {"BULLISH", "BEARISH"}
    ]
    if len(directional_momentum) == 2 and directional_momentum[0] != directional_momentum[1]:
        momentum = "MIXED"
    elif directional_momentum:
        momentum = directional_momentum[0]
    elif rsi_direction == "NEUTRAL" and macd_direction == "NEUTRAL":
        momentum = "NEUTRAL"

    directional = [
        item for item in (trend, rsi_direction, macd_direction)
        if item in {"BULLISH", "BEARISH"}
    ]
    if len(directional) >= 2 and all(item == directional[0] for item in directional):
        confluence = directional[0]
    elif len(set(directional)) > 1:
        confluence = "MIXED"
        reasons.append("Trend and momentum indicators conflict")
    else:
        confluence = "INSUFFICIENT_DATA"

    return IndicatorAssessment(
        symbol=symbol,
        timeframe=timeframe,
        status="HEALTHY",
        latest_close_time=timeframe_result.latest_close_time,
        ema_20=ema20,
        ema_50=ema50,
        rsi_14=rsi14,
        macd_line=macd_line,
        macd_signal=macd_signal,
        macd_histogram=macd_histogram,
        bollinger_mid=bollinger_mid,
        bollinger_upper=bollinger_upper,
        bollinger_lower=bollinger_lower,
        atr_14=atr14,
        atr_percent=atr_percent,
        vwap=vwap,
        relative_volume_20=relative_volume,
        trend=trend,
        momentum=momentum,
        confluence=confluence,
        reasons=tuple(reasons),
    )
