from datetime import datetime, timedelta, timezone

from backend.market_data_adapter import MarketCandle
from backend.market_assessment import assess_market


NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


def _candle(symbol, opened, closed, price, *, is_closed=True, volume=10.0):
    return MarketCandle(
        symbol=symbol, open_time=opened, close_time=closed,
        open=price - 0.2, high=price + 1.0, low=price - 1.0,
        close=price, volume=volume, quote_volume=price * volume,
        trades=10, is_closed=is_closed,
    )


def _fixed_series(interval_seconds, *, count=60, direction=1, gap_at=None):
    rows = []
    latest_open = NOW - timedelta(seconds=interval_seconds * (count + 1))
    for index in range(count):
        offset = index + (1 if gap_at is not None and index >= gap_at else 0)
        opened = latest_open + timedelta(seconds=interval_seconds * offset)
        closed = opened + timedelta(seconds=interval_seconds) - timedelta(milliseconds=1)
        price = 100.0 + direction * index * 0.5
        rows.append(_candle("BTCUSDT", opened, closed, price))
    return tuple(rows)


def test_assessment_separates_long_medium_and_short_timeframe_context():
    candles = {
        "1w": _fixed_series(7 * 86400, direction=-1),
        "1d": _fixed_series(86400, direction=1),
        "4h": _fixed_series(4 * 3600, direction=1),
        "1h": _fixed_series(3600, direction=1),
        "15m": _fixed_series(900, direction=1),
        "5m": _fixed_series(300, direction=1),
        "1m": _fixed_series(60, direction=1),
    }

    result = assess_market("BTCUSDT", candles, now=NOW)

    assert result.long_term_bias == "BEARISH"
    assert result.medium_term_bias == "BULLISH"
    assert result.short_term_bias == "BULLISH"
    assert result.direction == "MIXED"
    assert any("long-term and medium-term" in item.lower() for item in result.conflicts)
    assert result.orders_authorized is False


def test_assessment_marks_missing_and_in_progress_timeframes_insufficient():
    rows = list(_fixed_series(60))
    rows[-1] = _candle(
        "BTCUSDT", rows[-1].open_time, rows[-1].close_time,
        rows[-1].close, is_closed=False,
    )
    result = assess_market("BTCUSDT", {"1m": rows}, now=NOW)

    one_minute = next(item for item in result.timeframes if item.timeframe == "1m")
    five_minute = next(item for item in result.timeframes if item.timeframe == "5m")
    assert one_minute.status != "HEALTHY"
    assert one_minute.direction == "INSUFFICIENT_DATA"
    assert five_minute.status == "MISSING"
    assert result.orders_authorized is False


def test_assessment_rejects_gapped_duplicate_stale_and_future_candle_series():
    gapped = _fixed_series(60, gap_at=30)
    duplicate = list(_fixed_series(60))
    duplicate[40] = duplicate[39]
    fresh_rows = _fixed_series(60)
    stale = tuple(
        _candle("BTCUSDT", row.open_time - timedelta(days=2),
                row.close_time - timedelta(days=2), row.close)
        for row in fresh_rows
    )
    future = list(_fixed_series(60))
    last = future[-1]
    future[-1] = _candle(
        "BTCUSDT", NOW + timedelta(minutes=1), NOW + timedelta(minutes=2),
        last.close,
    )

    for rows in (gapped, tuple(duplicate), stale, tuple(future)):
        result = assess_market("BTCUSDT", {"1m": rows}, now=NOW)
        one_minute = next(item for item in result.timeframes if item.timeframe == "1m")
        assert one_minute.status != "HEALTHY"
        assert one_minute.direction == "INSUFFICIENT_DATA"


def test_month_and_year_are_distinct_calendar_timeframes():
    result = assess_market("BTCUSDT", {"1M": (), "1Y": ()}, now=NOW)
    labels = {item.timeframe for item in result.timeframes}
    assert "1m" in labels
    assert "1M" in labels
    assert "1Y" in labels

def test_authenticated_market_candle_api_includes_30m_monthly_and_derived_yearly_intervals():
    from pathlib import Path

    api_source = (Path(__file__).parents[1] / "app/engines/read_only_api.py").read_text(encoding="utf-8")
    assert 'analysis_intervals = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M")' in api_source
    assert "aggregate_complete_yearly_candles" in api_source

def test_assessment_detects_bullish_engulfing_and_volume_confirmed_breakout():
    rows = list(_fixed_series(3600, count=60, direction=0))
    previous = rows[-2]
    latest = rows[-1]
    rows[-2] = MarketCandle(
        symbol="BTCUSDT", open_time=previous.open_time, close_time=previous.close_time,
        open=101.0, high=102.0, low=98.0, close=99.0,
        volume=100.0, quote_volume=9900.0, trades=10, is_closed=True,
    )
    rows[-1] = MarketCandle(
        symbol="BTCUSDT", open_time=latest.open_time, close_time=latest.close_time,
        open=98.0, high=104.0, low=97.0, close=103.0,
        volume=300.0, quote_volume=30900.0, trades=30, is_closed=True,
    )

    result = assess_market("BTCUSDT", {"1h": rows}, now=NOW)
    hourly = next(item for item in result.timeframes if item.timeframe == "1h")

    assert hourly.status == "HEALTHY"
    assert hourly.candlestick_pattern == "BULLISH_ENGULFING"
    assert hourly.structure_event == "BREAKOUT_UP"
    assert result.market_phase == "BREAKOUT_UP"

def test_assessment_marks_bullish_reversal_attempt_inside_lower_structure():
    rows = list(_fixed_series(3600, count=60, direction=-1))
    previous = rows[-2]
    latest = rows[-1]
    rows[-2] = MarketCandle(
        symbol="BTCUSDT", open_time=previous.open_time, close_time=previous.close_time,
        open=102.0, high=103.0, low=99.0, close=100.0,
        volume=100.0, quote_volume=10000.0, trades=10, is_closed=True,
    )
    rows[-1] = MarketCandle(
        symbol="BTCUSDT", open_time=latest.open_time, close_time=latest.close_time,
        open=99.5, high=102.8, low=99.1, close=102.5,
        volume=150.0, quote_volume=15375.0, trades=15, is_closed=True,
    )

    result = assess_market("BTCUSDT", {"1h": rows}, now=NOW)
    hourly = next(item for item in result.timeframes if item.timeframe == "1h")

    assert hourly.candlestick_pattern == "BULLISH_ENGULFING"
    assert hourly.structure == "LOWER_HIGHS_LOWER_LOWS"
    assert hourly.structure_event == "REVERSAL_ATTEMPT_UP"
    assert result.market_phase == "REVERSAL_ATTEMPT_UP"
