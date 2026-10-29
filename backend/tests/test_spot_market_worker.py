from datetime import datetime, timedelta, timezone

import pytest

from backend.market_data_adapter import MarketCandle, SpotPriceSnapshot
from backend.spot_market_worker import SpotMarketDataWorker, SpotMarketWorkerConfig


NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def candles(symbol="BTCUSDT"):
    result = []
    for index in range(3):
        opened_at = NOW - timedelta(minutes=5 * (3 - index))
        result.append(MarketCandle(
            symbol=symbol, open_time=opened_at,
            close_time=opened_at + timedelta(minutes=5) - timedelta(milliseconds=1),
            open=100 + index, high=103 + index, low=99 + index,
            close=102 + index, volume=100, quote_volume=10000,
            trades=10, is_closed=True,
        ))
    return tuple(result)


class FakeSpotSource:
    def __init__(self, *, rows=None, price=None):
        self.rows = rows if rows is not None else candles()
        self.price = price if price is not None else SpotPriceSnapshot("BTCUSDT", 102.0, NOW, False)
        self.price_calls = 0

    def fetch_klines(self, symbol, *, interval, limit, now=None):
        return self.rows

    def fetch_spot_price(self, symbol):
        self.price_calls += 1
        return self.price


def test_spot_worker_emits_last_price_without_futures_fields():
    source = FakeSpotSource()
    result = SpotMarketDataWorker(
        source, SpotMarketWorkerConfig(("BTCUSDT",), interval="5m", candle_limit=10)
    ).poll_once(now=NOW)
    frame = result.frames[0]
    assert frame.status == "HEALTHY"
    assert frame.last_price == 102.0
    assert frame.market_type == "SPOT"
    assert not hasattr(frame, "mark_price")
    assert not hasattr(frame, "funding_rate")
    assert result.orders_authorized is False
    assert frame.orders_authorized is False


def test_spot_worker_rejects_candle_gap():
    rows = list(candles())
    last = rows[-1]
    rows[-1] = MarketCandle(
        symbol=last.symbol, open_time=last.open_time + timedelta(minutes=5),
        close_time=last.close_time + timedelta(minutes=5), open=last.open,
        high=last.high, low=last.low, close=last.close, volume=last.volume,
        quote_volume=last.quote_volume, trades=last.trades, is_closed=True,
    )
    result = SpotMarketDataWorker(
        FakeSpotSource(rows=tuple(rows)),
        SpotMarketWorkerConfig(("BTCUSDT",), interval="5m", candle_limit=10),
    ).poll_once(now=NOW)
    assert result.frames[0].status == "GAP_DETECTED"
    assert result.frames[0].last_price is None


def test_spot_worker_rejects_naive_clock():
    worker = SpotMarketDataWorker(
        FakeSpotSource(), SpotMarketWorkerConfig(("BTCUSDT",), interval="5m")
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        worker.poll_once(now=datetime(2026, 10, 29, 12, 0))
