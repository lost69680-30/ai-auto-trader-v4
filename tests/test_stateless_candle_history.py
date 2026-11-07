from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.engines.read_only_api import build_direct_market_history_payload
from backend import shared_market_worker


BASE = datetime(2026, 11, 1, tzinfo=timezone.utc)


def candle(index, *, symbol="BTCUSDT"):
    opened = BASE + timedelta(minutes=index)
    return SimpleNamespace(
        symbol=symbol, open_time=opened,
        close_time=opened + timedelta(minutes=1) - timedelta(milliseconds=1),
        open=100.0 + index, high=102.0 + index, low=99.0 + index,
        close=101.0 + index, volume=10.0, is_closed=True,
    )


def test_direct_history_fetches_from_exchange_and_marks_non_persistent():
    class Source:
        def __init__(self):
            self.calls = []

        def fetch_klines(self, symbol, *, interval, limit, now, start_time_ms, end_time_ms):
            self.calls.append((symbol, interval, limit, start_time_ms, end_time_ms))
            return (candle(0), candle(1))

    source = Source()
    result = build_direct_market_history_payload(
        source=source, symbol="BTCUSDT", allowed_symbols=("BTCUSDT",),
        start=BASE, end=BASE + timedelta(minutes=2), limit=10,
        now=BASE + timedelta(minutes=3),
    )

    assert len(result["candles"]) == 2
    assert result["history_source"] == "BINANCE_PUBLIC_REST_ON_DEMAND"
    assert result["persistent_candle_storage"] is False
    assert result["orders_authorized"] is False
    assert source.calls[0][1:3] == ("1m", 10)


def test_direct_history_rejects_gaps_instead_of_returning_incomplete_history():
    class Source:
        def fetch_klines(self, symbol, *, interval, limit, now, start_time_ms, end_time_ms):
            return (candle(0), candle(2))

    with pytest.raises(RuntimeError, match="missing or duplicate"):
        build_direct_market_history_payload(
            source=Source(), symbol="BTCUSDT", allowed_symbols=("BTCUSDT",),
            start=BASE, end=BASE + timedelta(minutes=3), limit=10,
            now=BASE + timedelta(minutes=4),
        )


def test_direct_history_rejects_symbols_outside_the_allowlist():
    with pytest.raises(ValueError, match="allowlist"):
        build_direct_market_history_payload(
            source=object(), symbol="AVAXUSDT", allowed_symbols=("BTCUSDT",),
            start=BASE, end=BASE + timedelta(minutes=2), limit=10,
            now=BASE + timedelta(minutes=3),
        )


def test_raw_candle_archive_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("PAPER_CANDLE_ARCHIVE_ENABLED", raising=False)
    assert shared_market_worker._env_bool("PAPER_CANDLE_ARCHIVE_ENABLED", default=False) is False


def test_raw_candle_archive_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("PAPER_CANDLE_ARCHIVE_ENABLED", "true")
    assert shared_market_worker._env_bool("PAPER_CANDLE_ARCHIVE_ENABLED", default=False) is True


def test_direct_history_reports_missing_trailing_coverage():
    class Source:
        def fetch_klines(self, symbol, *, interval, limit, now, start_time_ms, end_time_ms):
            return (candle(0),)

    result = build_direct_market_history_payload(
        source=Source(), symbol="BTCUSDT", allowed_symbols=("BTCUSDT",),
        start=BASE, end=BASE + timedelta(minutes=2), limit=10,
        now=BASE + timedelta(minutes=3),
    )

    assert result["complete"] is False
    assert result["truncated"] is False
    assert result["expected_candle_count"] == 2
    assert result["returned_candle_count"] == 1
    assert result["missing_candle_count"] == 1
    assert result["coverage_start"] == candle(0).open_time.isoformat()
    assert result["coverage_end"] == candle(0).open_time.isoformat()


def test_direct_history_reports_missing_leading_coverage():
    class Source:
        def fetch_klines(self, symbol, *, interval, limit, now, start_time_ms, end_time_ms):
            return (candle(1),)

    result = build_direct_market_history_payload(
        source=Source(), symbol="BTCUSDT", allowed_symbols=("BTCUSDT",),
        start=BASE, end=BASE + timedelta(minutes=2), limit=10,
        now=BASE + timedelta(minutes=3),
    )

    assert result["complete"] is False
    assert result["missing_candle_count"] == 1
    assert result["coverage_start"] == candle(1).open_time.isoformat()


def test_direct_history_marks_limit_truncation_explicitly():
    class Source:
        def fetch_klines(self, symbol, *, interval, limit, now, start_time_ms, end_time_ms):
            return (candle(0),)

    result = build_direct_market_history_payload(
        source=Source(), symbol="BTCUSDT", allowed_symbols=("BTCUSDT",),
        start=BASE, end=BASE + timedelta(minutes=2), limit=1,
        now=BASE + timedelta(minutes=3),
    )

    assert result["complete"] is False
    assert result["truncated"] is True
    assert result["expected_candle_count"] == 2
    assert result["returned_candle_count"] == 1
