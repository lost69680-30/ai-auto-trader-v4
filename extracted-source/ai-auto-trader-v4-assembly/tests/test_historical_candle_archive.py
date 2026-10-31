from datetime import datetime, timedelta, timezone

import pytest

from backend.historical_candle_archive import (
    ArchivedCandle,
    aggregate_candles,
    validate_minute_candle,
)


UTC = timezone.utc
BASE = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def minute(offset, *, opened=100.0, high=None, low=None, closed=True):
    start = BASE + timedelta(minutes=offset)
    return ArchivedCandle(
        venue="BINANCE_USDM_TESTNET",
        market_type="FUTURES",
        symbol="BTCUSDT",
        open_time=start,
        close_time=start + timedelta(minutes=1) - timedelta(milliseconds=1),
        open=opened,
        high=high,
        low=low,
        close=opened + 1.0,
        volume=10.0,
        is_closed=closed,
    )


def test_validates_closed_one_minute_candle_and_preserves_testnet_identity():
    candle = minute(0)

    validated = validate_minute_candle(candle, now=BASE + timedelta(minutes=2))

    assert validated.venue == "BINANCE_USDM_TESTNET"
    assert validated.market_type == "FUTURES"
    assert validated.symbol == "BTCUSDT"
    assert validated.is_closed is True


@pytest.mark.parametrize(
    "changes",
    [
        {"is_closed": False},
        {"volume": -1.0},
        {"high": 99.0},
        {"low": 103.0},
        {"close": float("nan")},
        {"open_time": BASE + timedelta(seconds=1)},
    ],
)
def test_rejects_invalid_or_unclosed_minute_candles(changes):
    values = {
        "venue": "BINANCE_USDM_TESTNET",
        "market_type": "FUTURES",
        "symbol": "BTCUSDT",
        "open_time": BASE,
        "close_time": BASE + timedelta(minutes=1) - timedelta(milliseconds=1),
        "open": 100.0,
        "high": 102.0,
        "low": 99.0,
        "close": 101.0,
        "volume": 10.0,
        "is_closed": True,
    }
    values.update(changes)
    candle = ArchivedCandle(**values)

    with pytest.raises(ValueError):
        validate_minute_candle(candle, now=BASE + timedelta(minutes=2))


def test_aggregates_only_complete_contiguous_five_minute_windows():
    rows = tuple(minute(i, opened=100.0 + i) for i in range(5))

    result = aggregate_candles(rows, interval="5m")

    assert len(result) == 1
    aggregate = result[0]
    assert aggregate.open_time == BASE
    assert aggregate.open == 100.0
    assert aggregate.close == 105.0
    assert aggregate.high == 106.0
    assert aggregate.low == 99.0
    assert aggregate.volume == 50.0


@pytest.mark.parametrize(
    "rows",
    [
        tuple(minute(i) for i in range(4)),
        (minute(0), minute(1), minute(2), minute(4), minute(5)),
    ],
)
def test_refuses_incomplete_or_gapped_five_minute_window(rows):
    with pytest.raises(ValueError):
        aggregate_candles(rows, interval="5m")


def test_aggregation_never_combines_different_venues_or_market_types():
    rows = [minute(i) for i in range(5)]
    rows[4] = ArchivedCandle(
        venue="BINANCE_USDM",
        market_type="FUTURES",
        symbol="BTCUSDT",
        open_time=rows[4].open_time,
        close_time=rows[4].close_time,
        open=100.0,
        high=102.0,
        low=99.0,
        close=101.0,
        volume=10.0,
        is_closed=True,
    )

    with pytest.raises(ValueError):
        aggregate_candles(tuple(rows), interval="5m")
