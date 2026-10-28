from datetime import datetime, timedelta, timezone

import pytest

from backend.signal_watcher import (
    MarketTick,
    SignalWatcher,
    WatchRequest,
    WatcherConfig,
)


NOW = datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc)


def watch(**overrides):
    values = dict(
        watch_id="watch-1",
        signal_id="signal-1",
        symbol="BTCUSDT",
        direction="LONG",
        trigger_price=100.0,
        trigger_rule="AT_OR_ABOVE",
        created_at=NOW - timedelta(seconds=5),
        expires_at=NOW + timedelta(minutes=10),
        invalidation_price=95.0,
    )
    values.update(overrides)
    return WatchRequest(**values)


def tick(price=99.0, **overrides):
    values = dict(symbol="BTCUSDT", price=price, observed_at=NOW)
    values.update(overrides)
    return MarketTick(**values)


def test_waits_until_price_trigger_then_notifies_once():
    watcher = SignalWatcher()
    watcher.add(watch())

    first = watcher.evaluate(tick(99.5), now=NOW)
    assert first.status == "WAITING"
    assert first.notify is False

    triggered = watcher.evaluate(tick(100.0), now=NOW)
    assert triggered.status == "TRIGGERED"
    assert triggered.notify is True

    repeated = watcher.evaluate(tick(101.0), now=NOW)
    assert repeated.status == "TRIGGERED"
    assert repeated.notify is False


def test_candle_close_rule_does_not_trigger_on_intrabar_touch():
    watcher = SignalWatcher()
    watcher.add(watch(trigger_rule="CANDLE_CLOSE_ABOVE"))

    result = watcher.evaluate(tick(101.0, candle_close=99.8), now=NOW)
    assert result.status == "WAITING"
    assert result.notify is False

    result = watcher.evaluate(tick(101.0, candle_close=100.1), now=NOW)
    assert result.status == "TRIGGERED"
    assert result.notify is True


def test_invalidated_watch_never_triggers():
    watcher = SignalWatcher()
    watcher.add(watch())

    result = watcher.evaluate(tick(94.9), now=NOW)
    assert result.status == "INVALIDATED"
    assert result.notify is True

    later = watcher.evaluate(tick(101.0), now=NOW)
    assert later.status == "INVALIDATED"
    assert later.notify is False


def test_expired_watch_cannot_trigger():
    watcher = SignalWatcher()
    request = watch(expires_at=NOW - timedelta(seconds=1))
    watcher.add(request)

    result = watcher.evaluate(tick(110), now=NOW)
    assert result.status == "EXPIRED"
    assert result.notify is True


def test_stale_market_data_fails_closed():
    watcher = SignalWatcher(config=WatcherConfig(max_market_data_age_seconds=5))
    watcher.add(watch())

    result = watcher.evaluate(tick(101, observed_at=NOW - timedelta(seconds=6)), now=NOW)
    assert result.status == "WAIT_RECHECK"
    assert result.notify is False


def test_wrong_symbol_does_not_update_watch():
    watcher = SignalWatcher()
    watcher.add(watch())

    with pytest.raises(ValueError, match="symbol"):
        watcher.evaluate(tick(101, symbol="ETHUSDT"), now=NOW)

    assert watcher.get_status("watch-1") == "WAITING"


def test_duplicate_watch_id_is_rejected():
    watcher = SignalWatcher()
    watcher.add(watch())
    with pytest.raises(ValueError, match="duplicate"):
        watcher.add(watch())


def test_invalid_watch_configuration_is_rejected():
    with pytest.raises(ValueError, match="trigger rule"):
        watch(trigger_rule="MAGIC")


def test_future_market_timestamp_fails_closed():
    watcher = SignalWatcher()
    watcher.add(watch())
    result = watcher.evaluate(tick(101, observed_at=NOW + timedelta(seconds=10)), now=NOW)
    assert result.status == "WAIT_RECHECK"
    assert result.notify is False


def test_one_tick_can_evaluate_multiple_watches_for_same_symbol():
    watcher = SignalWatcher()
    watcher.add(watch(watch_id="watch-a", trigger_price=100.0))
    watcher.add(watch(watch_id="watch-b", trigger_price=105.0, invalidation_price=98.0))

    results = watcher.evaluate_all(tick(101.0), now=NOW)
    assert [result.watch_id for result in results] == ["watch-a", "watch-b"]
    assert [result.status for result in results] == ["TRIGGERED", "WAITING"]
    assert [result.notify for result in results] == [True, False]
