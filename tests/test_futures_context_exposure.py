from datetime import datetime, timedelta, timezone
import math

from app.engines.read_only_api import MarketFeedCache
from backend.market_data_adapter import (
    BinanceSpotMarketDataAdapter,
    MarketCandle,
    MarkPriceSnapshot,
)
from backend.market_worker import FeedStatus, MarketDataWorker, MarketWorkerConfig
from backend.streaming_contract import make_market_snapshot_event


NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def candle(open_time, close_time):
    return MarketCandle(
        symbol="BTCUSDT",
        open_time=open_time,
        close_time=close_time,
        open=100.0,
        high=102.0,
        low=99.0,
        close=101.0,
        volume=12.0,
        quote_volume=1200.0,
        trades=22,
        is_closed=True,
    )


class FakeAdapter:
    def __init__(self, mark, *, clock=NOW):
        self.mark = mark
        self.clock = clock

    def fetch_klines(self, symbol, *, interval, limit, now=None):
        start = (now or self.clock) - timedelta(minutes=2)
        return (candle(start, start + timedelta(minutes=1)),)

    def fetch_mark_price(self, symbol):
        return self.mark


def config():
    return MarketWorkerConfig(
        symbols=("BTCUSDT",),
        interval="1m",
        candle_limit=30,
        poll_interval_seconds=5,
        max_market_age_seconds=90,
    )


def test_futures_mark_index_and_funding_context_reaches_read_only_api():
    worker = MarketDataWorker(
        FakeAdapter(MarkPriceSnapshot(
            symbol="BTCUSDT",
            mark_price=101.25,
            index_price=101.10,
            last_funding_rate=0.0001,
            next_funding_at=NOW + timedelta(hours=8),
            observed_at=NOW,
            orders_authorized=False,
        )),
        config(),
    )

    poll = worker.poll_once(now=NOW)
    frame = poll.frames[0]

    assert frame.status is FeedStatus.HEALTHY
    assert frame.mark_price == 101.25
    assert frame.index_price == 101.10
    assert frame.funding_rate == 0.0001
    assert frame.next_funding_at == NOW + timedelta(hours=8)
    assert frame.orders_authorized is False

    cache = MarketFeedCache(max_age_seconds=86400)
    cache.publish(poll)
    item = cache.items()[0]
    assert item["price_type"] == "FUTURES_MARK_PRICE"
    assert item["market_type"] == "FUTURES"
    assert item["mark_price"] == 101.25
    assert item["index_price"] == 101.10
    assert item["funding_rate"] == 0.0001
    assert item["next_funding_at"] == (NOW + timedelta(hours=8)).isoformat()
    assert item["orders_authorized"] is False

    event = make_market_snapshot_event(item, now=NOW)
    assert event["market_type"] == "FUTURES"
    assert event["mark_price"] == 101.25
    assert event["index_price"] == 101.10
    assert event["funding_rate"] == 0.0001
    assert event["next_funding_at"] == (NOW + timedelta(hours=8)).isoformat()
    assert event["orders_authorized"] is False


def test_worker_rejects_nonfinite_futures_funding_context():
    opened = NOW - timedelta(minutes=2)
    source = FakeAdapter(MarkPriceSnapshot(
        symbol="BTCUSDT",
        mark_price=101,
        index_price=100.9,
        last_funding_rate=math.nan,
        next_funding_at=NOW + timedelta(hours=8),
        observed_at=NOW,
    ))
    worker = MarketDataWorker(source, config())

    result = worker.poll_once(now=NOW)

    assert result.frames[0].status is FeedStatus.INVALID_DATA
    assert result.frames[0].closed_candles == ()
    assert any("funding" in reason.lower() for reason in result.frames[0].reasons)
    assert result.orders_authorized is False


def test_worker_rejects_partial_futures_context():
    source = FakeAdapter(MarkPriceSnapshot(
        symbol="BTCUSDT",
        mark_price=101,
        index_price=100.9,
        last_funding_rate=None,
        next_funding_at=NOW + timedelta(hours=8),
        observed_at=NOW,
    ))
    result = MarketDataWorker(source, config()).poll_once(now=NOW)

    assert result.frames[0].status is FeedStatus.INVALID_DATA
    assert result.frames[0].closed_candles == ()
    assert any("context" in reason.lower() for reason in result.frames[0].reasons)
    assert result.orders_authorized is False


def test_worker_rejects_expired_next_funding_timestamp():
    source = FakeAdapter(MarkPriceSnapshot(
        symbol="BTCUSDT",
        mark_price=101,
        index_price=100.9,
        last_funding_rate=0.0001,
        next_funding_at=NOW - timedelta(seconds=1),
        observed_at=NOW,
    ))
    result = MarketDataWorker(source, config()).poll_once(now=NOW)

    assert result.frames[0].status is FeedStatus.STALE
    assert result.frames[0].closed_candles == ()
    assert any("next funding" in reason.lower() for reason in result.frames[0].reasons)
    assert result.orders_authorized is False


def test_worker_rejects_market_snapshot_that_claims_order_authority():
    source = FakeAdapter(MarkPriceSnapshot(
        symbol="BTCUSDT",
        mark_price=101,
        index_price=100.9,
        last_funding_rate=0.0001,
        next_funding_at=NOW + timedelta(hours=8),
        observed_at=NOW,
        orders_authorized=True,
    ))
    result = MarketDataWorker(source, config()).poll_once(now=NOW)

    assert result.frames[0].status is FeedStatus.INVALID_DATA
    assert result.frames[0].closed_candles == ()
    assert any("order" in reason.lower() for reason in result.frames[0].reasons)
    assert result.orders_authorized is False


def test_spot_adapter_does_not_fabricate_futures_funding_or_index_data():
    adapter = BinanceSpotMarketDataAdapter(
        transport=lambda path, params, timeout: {
            "symbol": "BTCUSDT",
            "lastPrice": "101.25",
        }
    )

    snapshot = adapter.fetch_mark_price("BTCUSDT")

    assert snapshot.mark_price == 101.25
    assert snapshot.index_price is None
    assert snapshot.last_funding_rate is None
    assert snapshot.next_funding_at is None
    assert snapshot.orders_authorized is False


def test_spot_snapshot_is_labeled_as_last_price_in_read_only_api():
    now = datetime.now(timezone.utc)
    spot_mark = BinanceSpotMarketDataAdapter(
        transport=lambda path, params, timeout: {
            "symbol": "BTCUSDT",
            "lastPrice": "101.25",
        }
    ).fetch_mark_price("BTCUSDT")
    worker = MarketDataWorker(
        FakeAdapter(spot_mark, clock=now),
        config(),
    )

    poll = worker.poll_once(now=now)
    assert poll.frames[0].status is FeedStatus.HEALTHY

    cache = MarketFeedCache(max_age_seconds=90)
    cache.publish(poll)
    item = cache.items()[0]

    assert item["price_type"] == "SPOT_LAST_PRICE"
    assert item["market_type"] == "SPOT"
    assert item["last_price"] == 101.25
    assert item["index_price"] is None
    assert item["funding_rate"] is None
    assert item["next_funding_at"] is None
    assert item["orders_authorized"] is False

    event = make_market_snapshot_event(item, now=now)
    assert event["market_type"] == "SPOT"
    assert event["last_price"] == 101.25
    assert "mark_price" not in event
    assert "funding_rate" not in event
    assert event["orders_authorized"] is False

def test_worker_rejects_futures_snapshot_missing_all_futures_context():
    source = FakeAdapter(MarkPriceSnapshot(
        symbol="BTCUSDT",
        mark_price=101,
        index_price=None,
        last_funding_rate=None,
        next_funding_at=None,
        observed_at=NOW,
    ))
    result = MarketDataWorker(source, config()).poll_once(now=NOW)

    assert result.frames[0].status is FeedStatus.INVALID_DATA
    assert result.frames[0].closed_candles == ()
    assert any("futures context" in reason.lower() for reason in result.frames[0].reasons)
    assert result.orders_authorized is False
