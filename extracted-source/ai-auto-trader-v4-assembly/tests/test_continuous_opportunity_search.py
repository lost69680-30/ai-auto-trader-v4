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

def test_continuous_search_supports_spot_testnet_as_a_separate_market_type():
    source = FakeSource()
    store = FakeStore()
    handler = continuous_opportunity_search(
        store, source, policies("BTCUSDT"), market_type="SPOT",
        venue="BINANCE_SPOT_TESTNET",
    )

    result = handler({"scope": "continuous_opportunity_search"}, "spot-search-1")

    assert result.symbols_scanned == 1
    assert store.payload["market_type"] == "SPOT"
    assert store.payload["venue"] == "BINANCE_SPOT_TESTNET"
    assert result.orders_authorized is False


def test_continuous_search_rejects_spot_futures_venue_mismatch():
    source = FakeSource()
    store = FakeStore()

    with pytest.raises(ValueError, match="venue"):
        continuous_opportunity_search(
            store, source, policies("BTCUSDT"), market_type="SPOT",
            venue="BINANCE_USDM_TESTNET",
        )



def test_scan_registers_only_independently_eligible_assessments_when_atr_plan_is_ready():
    from backend.market_data_adapter import MarketCandle
    from backend.scanner import Contract
    from backend.strategy_assessment_engine import StrategyAssessmentRecord
    from backend.strategy_profiles import DEFAULT_STRATEGY_PROFILES

    class PlanningSource(FakeSource):
        def fetch_market_universe(self, *, now=None):
            base = super().fetch_market_universe(now=now)
            return SimpleNamespace(
                snapshots=base.snapshots,
                contracts=(Contract(
                    "BTCUSDT", "TRADING", "PERPETUAL", "USDT",
                    tick_size=0.01, quantity_step=0.001, min_quantity=0.001,
                ),),
            )

        def fetch_mark_price(self, symbol):
            return SimpleNamespace(
                symbol=symbol, mark_price=102.0, observed_at=datetime.now(timezone.utc),
                market_type="FUTURES", orders_authorized=False,
            )

        def fetch_klines(self, symbol, *, interval, limit, now=None):
            self.calls.append((symbol, interval, limit))
            if interval != "1h":
                return ()
            stamp = now or datetime.now(timezone.utc)
            rows = []
            for index in range(20):
                opened = stamp - timedelta(hours=20 - index)
                close = 100.0 + index * 0.1
                rows.append(MarketCandle(
                    symbol=symbol, open_time=opened,
                    close_time=opened + timedelta(hours=1) - timedelta(seconds=1),
                    open=close - 0.05, high=close + 0.5, low=close - 0.5,
                    close=close, volume=100.0, quote_volume=10_000.0,
                    trades=20, is_closed=True,
                ))
            return tuple(rows)

    class EligibleSwingEngine:
        def assess(self, candidate, evidence, *, now):
            records = []
            for profile in DEFAULT_STRATEGY_PROFILES:
                eligible = profile.strategy_id == "swing"
                records.append(StrategyAssessmentRecord(
                    assessment_id=f"scan-{profile.strategy_id}",
                    opportunity_id=candidate.opportunity_id,
                    strategy_id=profile.strategy_id,
                    strategy_version=profile.strategy_version,
                    assessment_version=candidate.assessment_version,
                    decision="ELIGIBLE" if eligible else (
                        "ANALYSIS_ONLY" if profile.analysis_only else "NO_TRADE"
                    ),
                    payload={
                        "strategy_id": profile.strategy_id,
                        "strategy_version": profile.strategy_version,
                        "direction": "LONG" if eligible else "NONE",
                        "required_timeframes": list(profile.required_timeframes),
                        "analysis_only": profile.analysis_only,
                        "eligible_for_risk_review": eligible,
                        "paper_eligible": eligible,
                        "orders_authorized": False,
                    },
                    evidence_refs=list(evidence),
                    orders_authorized=False,
                ))
            return tuple(records)

    class FakeCoordinator:
        market_type = "FUTURES"
        venue = "BINANCE_USDM_TESTNET"

        def __init__(self):
            self.registrations = []

        def register_assessed_opportunity(self, assessment, **kwargs):
            self.registrations.append((assessment, kwargs))
            return SimpleNamespace(opportunity_id="opp-test", state="WAITING")

    source = PlanningSource()
    store = FakeStore()
    coordinator = FakeCoordinator()
    handler = continuous_opportunity_search(
        store, source, policies("BTCUSDT"), market_type="FUTURES",
        strategy_engine=EligibleSwingEngine(),
        opportunity_coordinator=coordinator,
    )

    result = handler({"scope": "continuous_opportunity_search"}, "auto-register-run")

    assert result.opportunities_registered_count == 1
    assert result.opportunity_plan_failure_count == 0
    assert len(coordinator.registrations) == 1
    assessment, kwargs = coordinator.registrations[0]
    assert assessment.strategy_id == "swing"
    assert kwargs["timeframe"] == "1h"
    assert kwargs["market_type"] == "FUTURES"
    assert kwargs["venue"] == "BINANCE_USDM_TESTNET"
    assert kwargs["price_plan_result"].status == "READY"
    assert kwargs["price_plan_result"].orders_authorized is False
