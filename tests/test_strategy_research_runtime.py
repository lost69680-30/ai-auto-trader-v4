from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend import strategy_research_runtime
from backend.dataset_lineage import DatasetProvenance, candle_dataset_sha256
from backend.market_data_lineage import MarketDataIdentity, build_market_data_manifest
from backend.strategy_research import Candle
from backend.strategy_learning_lab import build_research_dataset
from backend.walk_forward import WalkForwardResult


NOW = datetime(2026, 11, 6, 5, 0, tzinfo=timezone.utc)


class FakeArchive:
    def __init__(self, rows):
        self.rows = rows
        self.request = None

    def latest_open_time(self, *, venue, market_type, symbol):
        return self.rows[-1].open_time if self.rows else None

    def read_closed_candles(self, **kwargs):
        self.request = kwargs
        return tuple(self.rows)


class FakeMemory:
    def __init__(self):
        self.items = []

    def append(self, experiment, *, idempotency_key):
        self.items.append((experiment, idempotency_key))
        return SimpleNamespace(experiment=experiment, duplicate=False)


def candle_rows(symbol="BTCUSDT", count=3000):
    from backend.historical_candle_archive import ArchivedCandle

    start = NOW - timedelta(minutes=count + 1)
    return [
        ArchivedCandle(
            venue="BINANCE_USDM_TESTNET", market_type="FUTURES", symbol=symbol,
            open_time=start + timedelta(minutes=i),
            close_time=start + timedelta(minutes=i, seconds=59, milliseconds=999),
            open=100.0 + i / 10000, high=101.0 + i / 10000,
            low=99.0 + i / 10000, close=100.5 + i / 10000,
            volume=10.0, is_closed=True, interval="1m",
        )
        for i in range(count)
    ]


def test_research_handler_persists_walk_forward_evidence_without_order_authority(monkeypatch):
    rows = candle_rows()
    archive = FakeArchive(rows)
    memory = FakeMemory()
    canonical_candles = tuple(Candle(
        symbol="BTCUSDT", timestamp=row.open_time,
        open=row.open, high=row.high, low=row.low, close=row.close, volume=row.volume,
    ) for row in rows)
    provenance = DatasetProvenance(
        provider="binance-public-testnet", venue="BINANCE_USDM_TESTNET",
        symbol="BTCUSDT", timeframe="1m",
        window_start=rows[0].open_time, window_end=rows[-1].open_time,
        candle_count=len(rows), data_sha256=candle_dataset_sha256(canonical_candles),
    )
    strategy = SimpleNamespace(
        initial_equity=1000.0, net_pnl=1.25, trade_count=4,
        win_rate_pct=50.0, max_drawdown_pct=2.5, total_costs=0.4,
        orders_authorized=False,
    )
    regime = SimpleNamespace(
        trend_state="UP", volatility_state="LOW",
        trend_return_pct=1.2, realized_volatility_pct=0.1,
        observations=499, methodology="test close-to-close regime evidence",
        orders_authorized=False,
    )
    window = SimpleNamespace(
        name="walk_forward_1", start_index=2000, end_index=2500,
        strategy=strategy, market_regime=regime, buy_hold_return_pct=0.5,
    )
    holdout = SimpleNamespace(
        name="untouched_holdout", start_index=2500, end_index=3000,
        strategy=strategy, market_regime=regime, buy_hold_return_pct=0.25,
    )
    result = WalkForwardResult(
        symbol="BTCUSDT", dataset_provenance=provenance, folds=(window,),
        holdout=holdout, warnings=("holdout is not used for selection",),
        orders_authorized=False,
    )
    calls = []

    def fake_evaluate(symbol, candles, **kwargs):
        calls.append((symbol, len(candles), kwargs))
        return result

    monkeypatch.setattr(strategy_research_runtime, "evaluate_walk_forward", fake_evaluate)
    handler = strategy_research_runtime.build_strategy_research_handler(
        archive, memory, symbols=("BTCUSDT",), clock=lambda: NOW,
    )

    summary = handler({
        "scope": "strategy_research",
        "market_type": "FUTURES",
        "venue": "BINANCE_USDM_TESTNET",
    }, "scheduled-research-1")

    assert summary["evaluated"] == 1
    assert summary["candidate_evaluations"] == 4
    assert summary["orders_authorized"] is False
    assert calls[0][0:2] == ("BTCUSDT", 3000)
    assert {call[2]["rule_spec"].family for call in calls} == {
        "SMA_CROSS", "MOMENTUM", "BREAKOUT", "RSI_THRESHOLD",
    }
    experiments = [item[0] for item in memory.items]
    assert len(experiments) == 4
    assert len({item.candidate_id for item in experiments}) == 4
    assert all(item.orders_authorized is False for item in experiments)
    assert all(item.outcomes["validation_quality"]["status"] == "INSUFFICIENT_DATA" for item in experiments)
    experiment, key = memory.items[0]
    expected_manifest = build_market_data_manifest(
        canonical_candles,
        MarketDataIdentity(
            venue="BINANCE_USDM_TESTNET", market_type="FUTURES",
            symbol="BTCUSDT", interval="1m", source_id="binance-public-testnet",
        ),
    )
    assert experiment.assumptions["market_data_manifest"] == expected_manifest.to_dict()
    assert experiment.source_provenance_id != ""
    assert experiment.orders_authorized is False
    assert experiment.status == "EVALUATED"
    assert experiment.assumptions["dataset_manifest"]["market_type"] == "FUTURES"
    assert experiment.assumptions["dataset_manifest"]["interval"] == "1m"
    assert experiment.outcomes["holdout"]["name"] == "untouched_holdout"
    assert experiment.outcomes["holdout"]["used_for_selection"] is False
    assert experiment.outcomes["orders_authorized"] is False
    assert experiment.outcomes["holdout_used_for_selection"] is False
    assert experiment.outcomes["untouched_holdout"]["used_for_selection"] is False
    assert experiment.outcomes["validation_folds"][0]["market_regime"]["orders_authorized"] is False
    dataset = build_research_dataset(tuple(experiments))
    assert dataset.market_data_manifests[0].sha256 == expected_manifest.sha256
    assert dataset.orders_authorized is False
    assert key.startswith("solo-research:")


def test_research_handler_rejects_wrong_scope_and_venue():
    handler = strategy_research_runtime.build_strategy_research_handler(
        FakeArchive([]), FakeMemory(), symbols=("BTCUSDT",), clock=lambda: NOW,
    )
    with pytest.raises(ValueError, match="scope"):
        handler({"scope": "opportunity_reassessment"}, "job-1")
    with pytest.raises(ValueError, match="venue"):
        handler({
            "scope": "strategy_research", "market_type": "FUTURES",
            "venue": "BINANCE_SPOT_TESTNET",
        }, "job-2")


def test_research_handler_skips_symbols_without_sufficient_archive_history(monkeypatch):
    archive = FakeArchive(candle_rows(count=100))
    memory = FakeMemory()
    handler = strategy_research_runtime.build_strategy_research_handler(
        archive, memory, symbols=("BTCUSDT",), candles_per_symbol=3000,
        clock=lambda: NOW,
    )
    with pytest.raises(RuntimeError, match="no candidates were evaluated"):
        handler({
            "scope": "strategy_research", "market_type": "FUTURES",
            "venue": "BINANCE_USDM_TESTNET",
        }, "job-3")
    assert memory.items == []


def test_research_memory_write_failure_propagates_for_scheduler_retry(monkeypatch):
    rows = candle_rows()
    archive = FakeArchive(rows)
    canonical_candles = tuple(Candle(
        symbol="BTCUSDT", timestamp=row.open_time,
        open=row.open, high=row.high, low=row.low, close=row.close, volume=row.volume,
    ) for row in rows)
    provenance = DatasetProvenance(
        provider="binance-public-testnet", venue="BINANCE_USDM_TESTNET",
        symbol="BTCUSDT", timeframe="1m",
        window_start=rows[0].open_time, window_end=rows[-1].open_time,
        candle_count=len(rows), data_sha256=candle_dataset_sha256(canonical_candles),
    )
    strategy = SimpleNamespace(
        initial_equity=1000.0, net_pnl=0.0, trade_count=0,
        win_rate_pct=0.0, max_drawdown_pct=0.0, total_costs=0.0,
        orders_authorized=False,
    )
    regime = SimpleNamespace(
        trend_state="RANGE", volatility_state="LOW",
        trend_return_pct=0.0, realized_volatility_pct=0.0,
        observations=499, methodology="test close-to-close regime evidence",
        orders_authorized=False,
    )
    window = SimpleNamespace(
        name="fold", start_index=2000, end_index=2500,
        strategy=strategy, market_regime=regime, buy_hold_return_pct=0.0,
    )
    result = WalkForwardResult(
        symbol="BTCUSDT", dataset_provenance=provenance, folds=(window,),
        holdout=window, warnings=(), orders_authorized=False,
    )

    class BrokenMemory:
        def append(self, experiment, *, idempotency_key):
            raise ValueError("database write unavailable")

    monkeypatch.setattr(
        strategy_research_runtime, "evaluate_walk_forward",
        lambda *args, **kwargs: result,
    )
    handler = strategy_research_runtime.build_strategy_research_handler(
        archive, BrokenMemory(), symbols=("BTCUSDT",), clock=lambda: NOW,
    )
    with pytest.raises(ValueError, match="database write unavailable"):
        handler({
            "scope": "strategy_research", "market_type": "FUTURES",
            "venue": "BINANCE_USDM_TESTNET",
        }, "job-write-failure")



def test_research_handler_rejects_evaluator_provenance_for_wrong_symbol(monkeypatch):
    rows = candle_rows()
    archive = FakeArchive(rows)
    memory = FakeMemory()
    wrong_provenance = DatasetProvenance(
        provider="binance-public-testnet", venue="BINANCE_USDM_TESTNET",
        symbol="ETHUSDT", timeframe="1m",
        window_start=rows[0].open_time, window_end=rows[-1].open_time,
        candle_count=len(rows), data_sha256="c" * 64,
    )
    strategy = SimpleNamespace(
        net_pnl=0.0, trade_count=0, win_rate_pct=0.0,
        max_drawdown_pct=0.0, total_costs=0.0,
    )
    window = SimpleNamespace(
        name="fold", start_index=2000, end_index=2500,
        strategy=strategy, buy_hold_return_pct=0.0,
    )
    result = SimpleNamespace(
        dataset_provenance=wrong_provenance, folds=(window,), holdout=window,
        warnings=(), orders_authorized=False,
    )
    monkeypatch.setattr(strategy_research_runtime, "evaluate_walk_forward", lambda *args, **kwargs: result)
    handler = strategy_research_runtime.build_strategy_research_handler(
        archive, memory, symbols=("BTCUSDT",), clock=lambda: NOW,
    )
    with pytest.raises(RuntimeError, match="no candidates were evaluated"):
        handler({
            "scope": "strategy_research", "market_type": "FUTURES",
            "venue": "BINANCE_USDM_TESTNET",
        }, "wrong-symbol-provenance")
    assert memory.items == []
