"""Pure transformations for higher-timeframe market data.

Yearly candles are aggregated only from a complete set of twelve validated,
closed monthly candles. Missing or open months are never filled or estimated.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timezone
from math import isfinite
from typing import Iterable

from backend.market_data_adapter import MarketCandle


def aggregate_complete_yearly_candles(
    monthly_candles: Iterable[MarketCandle],
) -> list[dict[str, object]]:
    """Return completed calendar-year OHLCV candles derived from monthly bars.

    A year is returned only when one closed monthly candle exists for each
    month January through December and each month is UTC-calendar aligned.
    The output schema matches the authenticated Android candle API.
    """
    months_by_year: dict[int, dict[int, MarketCandle]] = defaultdict(dict)
    invalid_years: set[int] = set()

    for candle in monthly_candles:
        if not candle.is_closed:
            continue
        opened_at = candle.open_time
        closed_at = candle.close_time
        if opened_at.tzinfo is None or opened_at.utcoffset() is None:
            continue
        if closed_at.tzinfo is None or closed_at.utcoffset() is None:
            continue

        opened_at = opened_at.astimezone(timezone.utc)
        year = opened_at.year
        if (
            opened_at.month < 1
            or opened_at.day != 1
            or opened_at.hour != 0
            or opened_at.minute != 0
            or opened_at.second != 0
            or opened_at.microsecond != 0
        ):
            invalid_years.add(year)
            continue

        existing = months_by_year[year]
        if opened_at.month in existing:
            invalid_years.add(year)
            continue
        values = (candle.open, candle.high, candle.low, candle.close, candle.volume)
        if (
            not all(isfinite(value) for value in values)
            or min(candle.open, candle.high, candle.low, candle.close) <= 0
            or candle.high < max(candle.open, candle.close, candle.low)
            or candle.low > min(candle.open, candle.close, candle.high)
            or candle.volume < 0
        ):
            invalid_years.add(year)
            continue
        existing[opened_at.month] = candle

    result: list[dict[str, object]] = []
    for year in sorted(months_by_year):
        if year in invalid_years:
            continue
        month_map = months_by_year[year]
        if set(month_map) != set(range(1, 13)):
            continue

        rows = [month_map[month] for month in range(1, 13)]
        first = rows[0]
        last = rows[-1]
        result.append(
            {
                "open_time": first.open_time.astimezone(timezone.utc).isoformat(),
                "close_time": last.close_time.astimezone(timezone.utc).isoformat(),
                "open": first.open,
                "high": max(row.high for row in rows),
                "low": min(row.low for row in rows),
                "close": last.close,
                "volume": sum(row.volume for row in rows),
                "is_closed": True,
            }
        )
    return result
