from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from backend.market_data_adapter import MarketCandle
from backend.market_intelligence_scan import SCAN_INTERVALS, market_intelligence_scan
from backend.scanner import MarketSnapshot


SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800,
}


def fixed_series(symbol, interval, now, *, count=60, direction=1, add_open=False):
    seconds = SECONDS[interval]
    latest_open = now - timedelta(seconds=seconds * (count + 1))
    rows = []
    for index in range(count):
        opened = latest_open + timedelta(seconds=seconds * index)
        closed = opened + timedelta(seconds=seconds) - timedelta(milliseconds=1)
        price = 100.0 + direction * index * 0.5
        rows.append(MarketCandle(
            symbol=symbol, open_time=opened, close_time=closed,
            open=price - 0.2, high=price + 1, low=price - 1,
            close=price, volume=100.0, quote_volume=price * 100,
            trades=10, is_closed=True,
        ))
    if add_open:
        opened = now - timedelta(seconds=seconds)
        rows.append(MarketCandle(
            symbol=symbol, open_time=opened, close_time=opened + timedelta(seconds=seconds) - timedelta(milliseconds=1),
            open=130.0, high=131.0, low=129.0, close=130.5,
            volume=100.0, quote_volume=13050.0, trades=10, is_closed=False,
        ))
    return tuple(rows)


def monthly_series(symbol, now, *, count=24, direction=1):
    current_month = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    months = []
    year, month = current_month.year, current_month.month - 1
    if month == 0:
        year -= 1
        month = 12
    for _ in range(count):
        months.append((year, month))
        month -= 1
        if month == 0:
            year -= 1
            month = 12
    months.reverse()
    rows = []
    for index, (year, month) in enumerate(months):
        opened = datetime(year, month, 1, tzinfo=timezone.utc)
        next_open = (
            datetime(year + 1, 1, 1, tzinfo=timezone.utc)
            if month == 12 else datetime(year, month + 1, 1, tzinfo=timezone.utc)
        )
        price = 100.0 + direction * index * 2.0
        rows.append(MarketCandle(
            symbol=symbol, open_time=opened,
            close_time=next_open - timedelta(milliseconds=1),
            open=price - 0.2, high=price + 1, low=price - 1,
            close=price, volume=100.0, quote_volume=price * 100,
            trades=10, is_closed=True,
        ))
    return tuple(rows)


class FakeSource:
    def __init__(self, *, stale_snapshot=False, open_interval=None):
        self.calls = []
        self.stale_snapshot = stale_snapshot
        self.open_interval = open_interval

    def fetch_market_universe(self, *, now=None):
        observed = now - timedelta(hours=1) if self.stale_snapshot else now
        return SimpleNamespace(snapshots=(
            MarketSnapshot("BTCUSDT", observed, 130.0, 20_000_000.0, 2.0, 1.0),
        ))

    def fetch_klines(self, symbol, *, interval, limit, now=None):
        self.calls.append((symbol, interval, limit))
        if interval == "1M":
            return monthly_series(symbol, now)
        return fixed_series(symbol, interval, now, add_open=(interval == self.open_interval))


class FakeStore:
    def __init__(self):
        self.payload = None

    def publish_intelligence(self, payload, *, now=None):
        assert payload["orders_authorized"] is False
        self.payload = payload


def policy(cost_pct=0.08):
    return SimpleNamespace(symbol="BTCUSDT", estimated_round_trip_cost_pct=cost_pct)


def test_scheduled_scan_publishes_assessment_and_adaptive_strategy_without_order_authority():
    source = FakeSource()
    store = FakeStore()
    handler = market_intelligence_scan(
        store, source, (policy(),), market_type="FUTURES", candle_limit=100,
    )

    handler({"scope": "market_intelligence"}, "market-scan-1")

    assert {call[1] for call in source.calls} == set(SCAN_INTERVALS)
    assert store.payload["scan_id"] == "market-scan-1"
    assert store.payload["market_type"] == "FUTURES"
    assert store.payload["orders_authorized"] is False
    item = store.payload["symbols"][0]
    assert item["symbol"] == "BTCUSDT"
    assert item["assessment"]["orders_authorized"] is False
    assert item["selection"]["orders_authorized"] is False
    assert item["selection"]["decision"] in {"SELECTED", "ANALYSIS_ONLY", "NO_TRADE"}


def test_scheduled_scan_records_in_progress_candles_but_excludes_them_from_confirmed_analysis():
    source = FakeSource(open_interval="5m")
    store = FakeStore()
    handler = market_intelligence_scan(store, source, (policy(),), market_type="FUTURES")

    handler({"scope": "market_intelligence"}, "market-scan-2")

    item = store.payload["symbols"][0]
    assert "5m" in item["in_progress_timeframes"]
    five_minute = next(row for row in item["assessment"]["timeframes"] if row["timeframe"] == "5m")
    assert five_minute["status"] == "HEALTHY"


def test_stale_liquidity_snapshot_fails_closed_to_no_trade():
    source = FakeSource(stale_snapshot=True)
    store = FakeStore()
    handler = market_intelligence_scan(store, source, (policy(),), market_type="FUTURES")

    handler({"scope": "market_intelligence"}, "market-scan-3")

    item = store.payload["symbols"][0]
    assert item["liquidity_evidence"]["snapshot_fresh"] is False
    assert item["selection"]["decision"] == "NO_TRADE"
    assert item["selection"]["execution_eligible"] is False


def test_scan_rejects_wrong_job_scope_and_invalid_configuration():
    import pytest

    with pytest.raises(ValueError, match="scope"):
        market_intelligence_scan(FakeStore(), FakeSource(), (policy(),), market_type="FUTURES")(
            {"scope": "orders"}, "job-1"
        )
    with pytest.raises(ValueError, match="market_type"):
        market_intelligence_scan(FakeStore(), FakeSource(), (policy(),), market_type="LIVE")
