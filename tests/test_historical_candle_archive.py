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
        high=opened + 2.0 if high is None else high,
        low=opened - 1.0 if low is None else low,
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


def test_archive_poll_converts_healthy_futures_frames_to_testnet_minute_rows():
    from types import SimpleNamespace

    from backend.historical_candle_archive import archive_market_poll

    source = minute(0)
    market_candle = SimpleNamespace(
        symbol="BTCUSDT", open_time=source.open_time, close_time=source.close_time,
        open=source.open, high=source.high, low=source.low, close=source.close,
        volume=source.volume, is_closed=True,
    )
    frame = SimpleNamespace(
        symbol="BTCUSDT", market_type="FUTURES", status=SimpleNamespace(value="HEALTHY"),
        closed_candles=(market_candle,), orders_authorized=False,
    )
    poll = SimpleNamespace(frames=(frame,), orders_authorized=False)
    inserted = []

    class Archive:
        def insert_closed_candles(self, candles, *, now):
            inserted.extend(candles)
            return len(candles)

    count = archive_market_poll(
        Archive(), poll, interval="1m", testnet=True,
        now=BASE + timedelta(minutes=2),
    )

    assert count == 1
    assert inserted[0].venue == "BINANCE_USDM_TESTNET"
    assert inserted[0].market_type == "FUTURES"
    assert inserted[0].symbol == "BTCUSDT"


def test_archive_poll_refuses_non_testnet_or_non_minute_source():
    from types import SimpleNamespace

    from backend.historical_candle_archive import archive_market_poll

    poll = SimpleNamespace(frames=(), orders_authorized=False)

    class Archive:
        def insert_closed_candles(self, candles, *, now):
            return 0

    with pytest.raises(ValueError, match="Testnet"):
        archive_market_poll(Archive(), poll, interval="1m", testnet=False, now=BASE + timedelta(minutes=2))
    with pytest.raises(ValueError, match="1m"):
        archive_market_poll(Archive(), poll, interval="5m", testnet=True, now=BASE + timedelta(minutes=2))


def test_backfill_paginates_from_last_archived_minute_and_recovers_gap():
    from types import SimpleNamespace

    from backend.historical_candle_archive import backfill_symbol_history

    class Archive:
        def __init__(self):
            self.latest = BASE - timedelta(minutes=1)
            self.inserted = []

        def latest_open_time(self, *, venue, market_type, symbol):
            return self.latest

        def insert_closed_candles(self, candles, *, now):
            rows = tuple(candles)
            self.inserted.extend(rows)
            if rows:
                self.latest = max(row.open_time for row in rows)
            return len(rows)

    class Source:
        def __init__(self):
            self.calls = []

        def fetch_klines(self, symbol, *, interval, limit, now, start_time_ms, end_time_ms):
            self.calls.append((symbol, interval, limit, start_time_ms, end_time_ms))
            all_rows = [minute(i) for i in range(5)]
            return tuple(
                SimpleNamespace(
                    symbol=symbol, open_time=row.open_time, close_time=row.close_time,
                    open=row.open, high=row.high, low=row.low, close=row.close,
                    volume=row.volume, is_closed=True,
                )
                for row in all_rows
                if int(row.open_time.timestamp() * 1000) >= start_time_ms
                and int(row.open_time.timestamp() * 1000) <= end_time_ms
            )[:limit]

    archive = Archive()
    source = Source()
    count = backfill_symbol_history(
        archive, source, symbol="BTCUSDT", market_type="FUTURES",
        testnet=True, now=BASE + timedelta(minutes=6), page_size=2,
    )

    assert count == 5
    assert len(source.calls) == 3
    assert [row.open_time for row in archive.inserted] == [BASE + timedelta(minutes=i) for i in range(5)]


def test_backfill_refuses_production_or_unsupported_market_type():
    from types import SimpleNamespace

    from backend.historical_candle_archive import backfill_symbol_history

    class Archive:
        def latest_open_time(self, **kwargs):
            return None

    with pytest.raises(ValueError, match="Testnet"):
        backfill_symbol_history(Archive(), SimpleNamespace(), symbol="BTCUSDT",
                                market_type="FUTURES", testnet=False, now=BASE)
