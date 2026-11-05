from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from backend.indicator_intelligence import assess_indicators
from backend.market_data_adapter import MarketCandle


NOW = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)


def candle_series(count=60):
    rows = []
    start = NOW - timedelta(hours=count + 1)
    for index in range(count):
        opened = start + timedelta(hours=index)
        close = 100.0 + index * 0.5 if index < 30 else 115.0 + (index - 30) * 1.0
        rows.append(MarketCandle(
            symbol="BTCUSDT",
            open_time=opened,
            close_time=opened + timedelta(hours=1) - timedelta(milliseconds=1),
            open=close - 0.25,
            high=close + 1.0,
            low=close - 1.0,
            close=close,
            volume=200.0 if index == count - 1 else 100.0,
            quote_volume=close * (200.0 if index == count - 1 else 100.0),
            trades=10,
            is_closed=True,
        ))
    return tuple(rows)


def test_healthy_candles_produce_auditable_indicator_values_and_paper_only_confluence():
    rows = candle_series()
    result = assess_indicators("BTCUSDT", "1h", rows, now=NOW)

    assert result.status == "HEALTHY"
    assert result.ema_20 is not None
    assert result.ema_50 is not None
    assert rows[-1].close > result.ema_20 > result.ema_50
    assert result.rsi_14 == pytest.approx(100.0)
    assert result.macd_line is not None and result.macd_line > 0
    assert result.macd_signal is not None
    assert result.macd_histogram is not None
    assert result.bollinger_lower < result.bollinger_mid < result.bollinger_upper
    assert result.atr_14 == pytest.approx(2.0)
    assert result.atr_percent > 0
    assert result.vwap is not None
    assert result.relative_volume_20 == pytest.approx(2.0)
    assert result.confluence == "BULLISH"
    assert result.calibration_status == "NOT_CALIBRATED"
    assert result.orders_authorized is False


def test_gapped_or_stale_candles_block_indicator_calculation():
    rows = list(candle_series())
    rows[30] = replace(
        rows[30],
        open_time=rows[30].open_time + timedelta(hours=1),
        close_time=rows[30].close_time + timedelta(hours=1),
    )

    result = assess_indicators("BTCUSDT", "1h", rows, now=NOW)

    assert result.status != "HEALTHY"
    assert result.ema_20 is None
    assert result.rsi_14 is None
    assert result.confluence == "INSUFFICIENT_DATA"
    assert result.orders_authorized is False


def test_short_history_reports_unavailable_indicators_without_inventing_values():
    rows = candle_series(count=30)
    result = assess_indicators("BTCUSDT", "1h", rows, now=NOW)

    assert result.status == "HEALTHY"
    assert result.ema_20 is not None
    assert result.ema_50 is None
    assert result.rsi_14 is not None
    assert result.macd_line is not None
    assert result.macd_signal is None
    assert "EMA50" in " ".join(result.reasons)
    assert result.calibration_status == "NOT_CALIBRATED"
    assert result.orders_authorized is False