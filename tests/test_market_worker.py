from datetime import datetime, timedelta, timezone

from backend.market_data_adapter import MarketCandle, MarketDataError, MarkPriceSnapshot
from backend.market_worker import FeedStatus, MarketDataWorker, MarketWorkerConfig


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def candle(open_time, close_time, *, closed=True):
    return MarketCandle(
        symbol="BTCUSDT", open_time=open_time, close_time=close_time,
        open=100, high=102, low=99, close=101, volume=10,
        quote_volume=1000, trades=20, is_closed=closed,
    )


class FakeAdapter:
    def __init__(self, candles=None, mark=None, error=None):
        self.candles = candles or ()
        self.mark = mark or MarkPriceSnapshot(
            symbol="BTCUSDT", mark_price=101, index_price=100.9,
            last_funding_rate=0.0001, next_funding_at=NOW + timedelta(hours=8),
            observed_at=NOW,
        )
        self.error = error
        self.calls = []

    def fetch_klines(self, symbol, *, interval, limit, now=None):
        self.calls.append(("klines", symbol, interval, limit, now))
        if self.error:
            raise self.error
        return self.candles

    def fetch_mark_price(self, symbol):
        self.calls.append(("mark", symbol))
        if self.error:
            raise self.error
        return self.mark


def config(**changes):
    base = dict(symbols=("BTCUSDT",), interval="1m", candle_limit=10,
                poll_interval_seconds=5, max_market_age_seconds=90,
                max_retries=2, retry_backoff_seconds=1, max_retry_backoff_seconds=8)
    base.update(changes)
    return MarketWorkerConfig(**base)


def test_worker_emits_healthy_frame_with_closed_candles_only():
    opened = NOW - timedelta(minutes=2)
    adapter = FakeAdapter(candles=(
        candle(opened, opened + timedelta(minutes=1)),
        candle(NOW - timedelta(minutes=1), NOW, closed=False),
    ))
    worker = MarketDataWorker(adapter, config())

    result = worker.poll_once(now=NOW)

    frame = result.frames[0]
    assert frame.status is FeedStatus.HEALTHY
    assert len(frame.closed_candles) == 1
    assert frame.mark_price == 101
    assert frame.orders_authorized is False
    assert result.orders_authorized is False


def test_worker_rejects_stale_mark_price():
    stale = MarkPriceSnapshot(
        symbol="BTCUSDT", mark_price=101, index_price=100.9,
        last_funding_rate=0.0, next_funding_at=NOW + timedelta(hours=8),
        observed_at=NOW - timedelta(minutes=10),
    )
    opened = NOW - timedelta(minutes=2)
    worker = MarketDataWorker(FakeAdapter((candle(opened, opened + timedelta(minutes=1)),), stale), config())

    result = worker.poll_once(now=NOW)

    assert result.frames[0].status is FeedStatus.STALE
    assert result.frames[0].closed_candles == ()


def test_worker_detects_candle_gap_and_fails_closed():
    first = NOW - timedelta(minutes=4)
    second = NOW - timedelta(minutes=2)
    adapter = FakeAdapter(candles=(
        candle(first, first + timedelta(minutes=1)),
        candle(second, second + timedelta(minutes=1)),
    ))
    worker = MarketDataWorker(adapter, config())

    result = worker.poll_once(now=NOW)

    assert result.frames[0].status is FeedStatus.GAP_DETECTED
    assert result.frames[0].closed_candles == ()
    assert any("gap" in reason.lower() for reason in result.frames[0].reasons)


def test_worker_marks_network_failures_disconnected_and_tracks_retry_state():
    worker = MarketDataWorker(FakeAdapter(error=TimeoutError("offline")), config())

    first = worker.poll_once(now=NOW)
    second = worker.poll_once(now=NOW + timedelta(seconds=5))

    assert first.frames[0].status is FeedStatus.DISCONNECTED
    assert first.frames[0].retry_count == 1
    assert second.frames[0].retry_count == 2
    assert first.orders_authorized is False


def test_worker_rejects_invalid_config():
    import pytest
    with pytest.raises(ValueError):
        config(poll_interval_seconds=0)
    with pytest.raises(ValueError):
        config(symbols=("btcusdt",))


def test_worker_backoff_grows_on_disconnect_and_resets_after_recovery():
    adapter = FakeAdapter(error=TimeoutError("offline"))
    worker = MarketDataWorker(adapter, config(retry_backoff_seconds=2, max_retry_backoff_seconds=5, max_retries=3))

    worker.poll_once(now=NOW)
    assert worker.next_delay_seconds == 2
    worker.poll_once(now=NOW + timedelta(seconds=5))
    assert worker.next_delay_seconds == 4
    worker.poll_once(now=NOW + timedelta(seconds=10))
    assert worker.next_delay_seconds == 5

    adapter.error = None
    opened = NOW + timedelta(seconds=10) - timedelta(minutes=2)
    adapter.candles = (candle(opened, opened + timedelta(minutes=1)),)
    adapter.mark = MarkPriceSnapshot(
        symbol="BTCUSDT", mark_price=101, index_price=100.9,
        last_funding_rate=0.0, next_funding_at=NOW + timedelta(hours=8),
        observed_at=NOW + timedelta(seconds=10),
    )
    recovered = worker.poll_once(now=NOW + timedelta(seconds=10))

    assert recovered.frames[0].status is FeedStatus.HEALTHY
    assert worker.next_delay_seconds == worker.config.poll_interval_seconds
    assert recovered.frames[0].retry_count == 0


def test_run_forever_survives_consumer_callback_failure_and_continues_polling():
    class StopAfterTwoWaits:
        def __init__(self):
            self.waits = 0
            self.stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _seconds):
            self.waits += 1
            if self.waits >= 2:
                self.stopped = True

    opened = NOW - timedelta(minutes=2)
    adapter = FakeAdapter(candles=(candle(opened, opened + timedelta(minutes=1)),))
    worker = MarketDataWorker(adapter, config())
    stop = StopAfterTwoWaits()
    received = []

    def callback(result):
        received.append(result)
        if len(received) == 1:
            raise RuntimeError("consumer transient failure")

    worker.run_forever(stop, on_result=callback, clock=lambda: NOW)

    assert len(received) == 2
    assert len([call for call in adapter.calls if call[0] == "mark"]) == 2


def test_healthy_market_frame_carries_read_only_funding_context():
    opened = NOW - timedelta(minutes=2)
    source = FakeAdapter(candles=(candle(opened, opened + timedelta(minutes=1)),))
    worker = MarketDataWorker(source, config())

    poll = worker.poll_once(now=NOW)

    assert poll.frames[0].status is FeedStatus.HEALTHY
    assert poll.frames[0].funding_rate == 0.0001
    assert poll.frames[0].orders_authorized is False


def test_worker_preserves_safe_market_data_error_detail_for_diagnostics():
    worker = MarketDataWorker(
        FakeAdapter(error=MarketDataError("Binance returned HTTP 403 for /fapi/v1/klines")),
        config(),
    )

    result = worker.poll_once(now=NOW)

    assert result.frames[0].status is FeedStatus.DISCONNECTED
    assert result.frames[0].reasons == (
        "public market-data request failed: MarketDataError: "
        "Binance returned HTTP 403 for /fapi/v1/klines",
    )
    assert result.orders_authorized is False


def test_worker_validates_receive_timestamp_against_time_after_network_fetch():
    from datetime import datetime, timedelta, timezone

    request_started = datetime.now(timezone.utc)
    opened = request_started - timedelta(minutes=2)
    received_at = request_started + timedelta(milliseconds=25)
    source = FakeAdapter(candles=(candle(opened, opened + timedelta(minutes=1)),))
    source.mark = MarkPriceSnapshot(
        symbol="BTCUSDT", mark_price=101, index_price=100.9,
        last_funding_rate=0.0, next_funding_at=received_at + timedelta(hours=8),
        observed_at=received_at,
    )
    worker = MarketDataWorker(source, config())

    # Production mode (no explicit deterministic `now`) refreshes its
    # validation clock after the adapter has returned.
    result = worker.poll_once()

    assert result.frames[0].status is FeedStatus.HEALTHY
    assert result.frames[0].reasons == ()
    assert result.orders_authorized is False


def test_production_poll_does_not_pin_kline_validation_to_request_start_time():
    request_started = datetime.now(timezone.utc)
    opened = request_started - timedelta(minutes=2)
    source = FakeAdapter(candles=(candle(opened, opened + timedelta(minutes=1)),))
    source.mark = MarkPriceSnapshot(
        symbol="BTCUSDT", mark_price=101, index_price=100.9,
        last_funding_rate=0.0, next_funding_at=request_started + timedelta(hours=8),
        observed_at=datetime.now(timezone.utc),
    )
    worker = MarketDataWorker(source, config())

    result = worker.poll_once()

    assert result.frames[0].status is FeedStatus.HEALTHY
    kline_call = next(call for call in source.calls if call[0] == "klines")
    assert kline_call[-1] is None
    assert result.orders_authorized is False


def test_worker_rejects_nonfinite_futures_funding_context():
    import math

    opened = NOW - timedelta(minutes=2)
    source = FakeAdapter(candles=(candle(opened, opened + timedelta(minutes=1)),))
    source.mark = MarkPriceSnapshot(
        symbol="BTCUSDT", mark_price=101, index_price=100.9,
        last_funding_rate=math.nan, next_funding_at=NOW + timedelta(hours=8),
        observed_at=NOW,
    )

    result = MarketDataWorker(source, config()).poll_once(now=NOW)

    assert result.frames[0].status is FeedStatus.INVALID_DATA
    assert result.frames[0].closed_candles == ()
    assert any("funding" in reason.lower() for reason in result.frames[0].reasons)
    assert result.orders_authorized is False


def test_worker_rejects_partial_futures_context():
    opened = NOW - timedelta(minutes=2)
    source = FakeAdapter(candles=(candle(opened, opened + timedelta(minutes=1)),))
    source.mark = MarkPriceSnapshot(
        symbol="BTCUSDT", mark_price=101, index_price=100.9,
        last_funding_rate=None, next_funding_at=NOW + timedelta(hours=8),
        observed_at=NOW,
    )

    result = MarketDataWorker(source, config()).poll_once(now=NOW)

    assert result.frames[0].status is FeedStatus.INVALID_DATA
    assert result.frames[0].closed_candles == ()
    assert any("context" in reason.lower() for reason in result.frames[0].reasons)
    assert result.orders_authorized is False


def test_worker_rejects_future_funding_timestamp_that_is_not_in_future():
    opened = NOW - timedelta(minutes=2)
    source = FakeAdapter(candles=(candle(opened, opened + timedelta(minutes=1)),))
    source.mark = MarkPriceSnapshot(
        symbol="BTCUSDT", mark_price=101, index_price=100.9,
        last_funding_rate=0.0001, next_funding_at=NOW - timedelta(seconds=1),
        observed_at=NOW,
    )

    result = MarketDataWorker(source, config()).poll_once(now=NOW)

    assert result.frames[0].status is FeedStatus.INVALID_DATA
    assert result.frames[0].closed_candles == ()
    assert any("next funding" in reason.lower() for reason in result.frames[0].reasons)
    assert result.orders_authorized is False


def test_worker_rejects_mark_snapshot_that_claims_order_authority():
    opened = NOW - timedelta(minutes=2)
    source = FakeAdapter(candles=(candle(opened, opened + timedelta(minutes=1)),))
    source.mark = MarkPriceSnapshot(
        symbol="BTCUSDT", mark_price=101, index_price=100.9,
        last_funding_rate=0.0001, next_funding_at=NOW + timedelta(hours=8),
        observed_at=NOW, orders_authorized=True,
    )

    result = MarketDataWorker(source, config()).poll_once(now=NOW)

    assert result.frames[0].status is FeedStatus.INVALID_DATA
    assert result.frames[0].closed_candles == ()
    assert any("order" in reason.lower() for reason in result.frames[0].reasons)
    assert result.orders_authorized is False
