from datetime import datetime, timezone

from backend.market_data_adapter import MarketCandle
from backend.market_timeframes import aggregate_complete_yearly_candles


def monthly_candles(year: int, months=range(1, 13), *, open_months=()):
    rows = []
    for month in months:
        rows.append(
            MarketCandle(
                symbol="BTCUSDT",
                open_time=datetime(year, month, 1, tzinfo=timezone.utc),
                close_time=datetime(year + (month == 12), 1 if month == 12 else month + 1, 1, tzinfo=timezone.utc)
                if month != 12 else datetime(year + 1, 1, 1, tzinfo=timezone.utc),
                open=100.0 + month,
                high=120.0 + month,
                low=90.0 + month,
                close=110.0 + month,
                volume=float(month * 10),
                quote_volume=float(month * 1000),
                trades=month * 100,
                is_closed=month not in open_months,
            )
        )
    return rows


def test_yearly_candle_aggregates_only_complete_closed_calendar_year():
    rows = monthly_candles(2024)
    result = aggregate_complete_yearly_candles(rows)

    assert len(result) == 1
    candle = result[0]
    assert candle["open_time"] == "2024-01-01T00:00:00+00:00"
    assert candle["close_time"] == "2025-01-01T00:00:00+00:00"
    assert candle["open"] == 101.0
    assert candle["high"] == 132.0
    assert candle["low"] == 91.0
    assert candle["close"] == 122.0
    assert candle["volume"] == 780.0
    assert candle["is_closed"] is True


def test_yearly_candle_omits_incomplete_year_and_missing_months():
    partial_year = monthly_candles(2025, range(1, 10))
    missing_month = [c for c in monthly_candles(2024) if c.open_time.month != 6]

    assert aggregate_complete_yearly_candles(partial_year) == []
    assert aggregate_complete_yearly_candles(missing_month) == []


def test_yearly_candle_omits_year_with_any_unclosed_month():
    rows = monthly_candles(2024, open_months=(12,))
    assert aggregate_complete_yearly_candles(rows) == []


def test_yearly_candles_are_sorted_and_derived_from_monthly_rows():
    rows = monthly_candles(2023) + monthly_candles(2024)
    result = aggregate_complete_yearly_candles(rows)

    assert [candle["open_time"][:4] for candle in result] == ["2023", "2024"]
    assert all(candle["is_closed"] for candle in result)
