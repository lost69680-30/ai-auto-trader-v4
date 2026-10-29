from datetime import datetime, timedelta, timezone
import pytest
from backend.market_data_adapter import MarketCandle
from backend.spot_historical_research import SpotHistoricalResearchService

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)

class FakeSource:
    def __init__(self, rows): self.rows, self.calls = rows, []
    def fetch_klines(self, symbol, *, interval, limit, now):
        self.calls.append((symbol, interval, limit))
        return self.rows

def sample_rows(n=60):
    result = []
    for i in range(n):
        start = NOW - timedelta(minutes=5 * (n-i))
        price = 100 + i * .1
        result.append(MarketCandle("BTCUSDT", start, start+timedelta(minutes=5)-timedelta(milliseconds=1),
            price, price*1.01, price*.99, price, 100, 10000, 10, True))
    return tuple(result)

def test_report_is_spot_only_bounded_and_read_only():
    source = FakeSource(sample_rows())
    report = SpotHistoricalResearchService(source).report("BTCUSDT", interval="5m", limit=60, now=NOW)
    assert report["market_type"] == "SPOT"
    assert report["closed_candles"] == 60
    assert report["orders_authorized"] is False
    assert report["positions_created"] is False
    assert report["total_estimated_costs"] >= 0
    assert source.calls == [("BTCUSDT", "5m", 60)]

@pytest.mark.parametrize("kwargs", [
    {"symbol":"btcusdt"}, {"symbol":"BTCUSDT","limit":1501},
    {"symbol":"BTCUSDT","interval":"1M"}, {"symbol":"BTCUSDT","limit":True},
])
def test_invalid_requests_are_rejected(kwargs):
    with pytest.raises(ValueError):
        SpotHistoricalResearchService(FakeSource(sample_rows())).report(now=NOW, **kwargs)

def test_naive_clock_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        SpotHistoricalResearchService(FakeSource(sample_rows())).report("BTCUSDT", now=datetime(2026,10,29,12))
