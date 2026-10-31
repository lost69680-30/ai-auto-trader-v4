from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.market_data_adapter import MarketCandle
from backend.market_intelligence_scan import SCAN_INTERVALS
from backend.scanner import MarketSnapshot
from backend.continuous_opportunity_search import continuous_opportunity_search


NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


class FakeSource:
    def __init__(self, *, stale=False, symbols=("BTCUSDT",)):
        self.stale = stale
        self.symbols = tuple(symbols)
        self.calls = []

    def fetch_market_universe(self, *, now=None):
        observed = now - timedelta(hours=1) if self.stale else now
        return SimpleNamespace(snapshots=tuple(
            MarketSnapshot(symbol, observed, 130.0, 20_000_000.0, 2.0, 1.0)
            for symbol in self.symbols
        ))

    def fetch_klines(self, symbol, *, interval, limit, now=None):
        self.calls.append((symbol, interval, limit))
        return ()


class FakeStore:
    def __init__(self):
        self.payload = None

    def publish_intelligence(self, payload, *, now=None):
        assert payload["orders_authorized"] is False
        self.payload = payload


def policies(*symbols):
    return tuple(SimpleNamespace(
        symbol=symbol,
        estimated_round_trip_cost_pct=0.08,
    ) for symbol in symbols)


def test_continuous_search_persists_five_independent_assessments_per_symbol():
    source = FakeSource(symbols=("BTCUSDT", "ETHUSDT"))
    store = FakeStore()
    handler = continuous_opportunity_search(
        store, source, policies("BTCUSDT", "ETHUSDT"), market_type="FUTURES",
    )

    result = handler({"scope": "continuous_opportunity_search"}, "search-run-1")

    assert result.scan_id == "search-run-1"
    assert result.symbols_scanned == 2
    assert result.strategy_assessments_count == 10
    assert result.orders_authorized is False
    assert len(source.calls) == 2 * len(SCAN_INTERVALS)
    assert len(store.payload["symbols"]) == 2
    for symbol in store.payload["symbols"]:
        assessments = symbol["selection"]["strategy_assessments"]
        assert {item["strategy_id"] for item in assessments} == {
            "scalping", "day", "swing", "position", "long_term"
        }
        assert all(item["orders_authorized"] is False for item in assessments)
        assert all(item["payload"]["calibration_status"] == "NOT_CALIBRATED" for item in assessments)


def test_stale_universe_snapshot_cannot_create_eligible_strategy_candidates():
    source = FakeSource(stale=True)
    store = FakeStore()
    handler = continuous_opportunity_search(
        store, source, policies("BTCUSDT"), market_type="FUTURES",
    )

    result = handler({"scope": "continuous_opportunity_search"}, "stale-search")

    assert result.eligible_candidate_count == 0
    assessments = store.payload["symbols"][0]["selection"]["strategy_assessments"]
    assert all(
        item["payload"]["eligible_for_risk_review"] is False
        for item in assessments
    )
    assert all(item["orders_authorized"] is False for item in assessments)


def test_search_rejects_wrong_job_scope_and_live_environment():
    source = FakeSource()
    store = FakeStore()
    handler = continuous_opportunity_search(
        store, source, policies("BTCUSDT"), market_type="FUTURES",
    )

    with pytest.raises(ValueError, match="scope"):
        handler({"scope": "orders"}, "wrong-scope")

    with pytest.raises(ValueError, match="Testnet"):
        continuous_opportunity_search(
            store, source, policies("BTCUSDT"),
            market_type="FUTURES", environment="LIVE",
        )


def test_search_reuses_existing_source_and_bounds_fetch_concurrency():
    source = FakeSource(symbols=("BTCUSDT", "ETHUSDT"))
    store = FakeStore()
    handler = continuous_opportunity_search(
        store, source, policies("BTCUSDT", "ETHUSDT"),
        market_type="FUTURES", max_fetch_workers=2,
    )

    result = handler({"scope": "continuous_opportunity_search"}, "bounded-search")

    assert result.symbols_scanned == 2
    assert len(source.calls) == 2 * len(SCAN_INTERVALS)
    assert all(call[2] == 200 for call in source.calls)
