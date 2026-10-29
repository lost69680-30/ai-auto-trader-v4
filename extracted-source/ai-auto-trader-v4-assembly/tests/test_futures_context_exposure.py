from datetime import datetime, timedelta, timezone

from app.engines.read_only_api import MarketFeedCache
from backend.market_data_adapter import MarketCandle, MarkPriceSnapshot
from backend.market_worker import MarketDataWorker, MarketWorkerConfig


NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


class FuturesAdapter:
    def fetch_klines(self, symbol, *, interval, limit, now=None):
        start = NOW - timedelta(minutes=2)
        return (
            MarketCandle(
                symbol=symbol,
                open_time=start,
                close_time=start + timedelta(minutes=1),
                open=100.0, high=102.0, low=99.0, close=101.0,
                volume=12.0, quote_volume=1200.0, trades=22, is_closed=True,
            ),
        )

    def fetch_mark_price(self, symbol):
        return MarkPriceSnapshot(
            symbol=symbol,
            mark_price=101.25,
            index_price=101.10,
            last_funding_rate=0.0001,
            next_funding_at=NOW + timedelta(hours=8),
            observed_at=NOW,
            orders_authorized=False,
        )


def test_futures_mark_index_and_funding_context_reaches_read_only_api():
    worker = MarketDataWorker(
        FuturesAdapter(),
        MarketWorkerConfig(
            symbols=("BTCUSDT",), interval="1m", candle_limit=30,
            poll_interval_seconds=5, max_market_age_seconds=90,
        ),
    )
    poll = worker.poll_once(now=NOW)

    frame = poll.frames[0]
    assert frame.status.value == "HEALTHY"
    assert frame.mark_price == 101.25
    assert frame.index_price == 101.10
    assert frame.funding_rate == 0.0001
    assert frame.next_funding_at == NOW + timedelta(hours=8)
    assert frame.orders_authorized is False

    cache = MarketFeedCache(max_age_seconds=90)
    cache.publish(poll)
    item = cache.items()[0]
    assert item["mark_price"] == 101.25
    assert item["index_price"] == 101.10
    assert item["funding_rate"] == 0.0001
    assert item["next_funding_at"] == (NOW + timedelta(hours=8)).isoformat()
    assert item["orders_authorized"] is False
