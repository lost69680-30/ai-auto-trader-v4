from dataclasses import replace
from datetime import datetime, timedelta, timezone

from backend.market_assessment import assess_market
from backend.market_data_adapter import MarketCandle


NOW = datetime(2026, 11, 7, 8, 0, tzinfo=timezone.utc)


def _monthly_series(count=20):
    rows = []
    year, month = 2025, 3
    for index in range(count):
        opened = datetime(year, month, 1, tzinfo=timezone.utc)
        if month == 12:
            next_open = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            next_open = datetime(year, month + 1, 1, tzinfo=timezone.utc)
        closed = next_open - timedelta(milliseconds=1)
        price = 100.0 + index
        rows.append(MarketCandle(
            symbol="BTCUSDT",
            open_time=opened,
            close_time=closed,
            open=price - 0.2,
            high=price + 1.0,
            low=price - 1.0,
            close=price,
            volume=1000.0,
            quote_volume=price * 1000.0,
            trades=100,
            is_closed=True,
        ))
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return rows


def test_monthly_history_rejects_extreme_price_discontinuity_instead_of_reporting_healthy():
    rows = _monthly_series()
    previous = rows[-2]
    latest = rows[-1]
    rows[-1] = MarketCandle(
        symbol="BTCUSDT",
        open_time=latest.open_time,
        close_time=latest.close_time,
        open=previous.close,
        high=previous.close * 25.0,
        low=previous.close * 0.99,
        close=latest.close,
        volume=latest.volume,
        quote_volume=latest.quote_volume,
        trades=latest.trades,
        is_closed=True,
    )

    result = assess_market("BTCUSDT", {"1M": rows}, now=NOW)
    monthly = next(item for item in result.timeframes if item.timeframe == "1M")

    assert monthly.status == "INVALID_DATA"
    assert monthly.direction == "INSUFFICIENT_DATA"
    assert any("extreme candle price discontinuity" in reason for reason in monthly.reasons)
    assert result.orders_authorized is False


def test_higher_timeframe_candle_inside_conservative_continuity_bound_remains_usable():
    rows = _monthly_series()
    previous = rows[-2]
    latest = rows[-1]
    rows[-1] = replace(
        latest,
        open=previous.close,
        high=previous.close * 5.9,
        low=previous.close * 0.99,
    )

    result = assess_market("BTCUSDT", {"1M": rows}, now=NOW)
    monthly = next(item for item in result.timeframes if item.timeframe == "1M")

    assert monthly.status == "HEALTHY"
    assert monthly.direction in {"BULLISH", "BEARISH", "NEUTRAL"}
    assert result.orders_authorized is False
