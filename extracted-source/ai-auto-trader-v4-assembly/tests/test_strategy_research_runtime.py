from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend import strategy_research_runtime
from backend.dataset_lineage import DatasetProvenance


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
    provenance = DatasetProvenance(
        provider="binance-public-testnet", venue="BINANCE_USDM_TESTNET",
        symbol="BTCUSDT", timeframe="1m",
        window_start=rows[0].open_time, window_end=rows[-1].open_time,
        candle_count=len(rows), data_sha256="a" * 64,
    )
    strategy = SimpleNamespace(
        net_pnl=1.25, trade_count=4, win_rate_pct=50.0,
        max_drawdown_pct=2.5, total_costs=0.4,
    )
    window = SimpleNamespace(
        name="walk_forward_1", start_index=2000, end_index=2500,
        strategy=strategy, buy_hold_return_pct=0.5,
    )
    holdout = SimpleNamespace(
        name="untouched_holdout", start_index=2500, end_index=3000,
        strategy=strategy, buy_hold_return_pct=0.25,
    )
    result = SimpleNamespace(
        dataset_provenance=provenance, folds=(window,), holdout=holdout,
        warnings=("holdout is not used for selection",), orders_authorized=False,
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
    assert summary["orders_authorized"] is False
    assert calls[0][0:2] == ("BTCUSDT", 3000)
    experiment, key = memory.items[0]
    assert experiment.orders_authorized is False
    assert experiment.status == "EVALUATED"
    assert experiment.assumptions["dataset_manifest"]["market_type"] == "FUTURES"
    assert experiment.assumptions["dataset_manifest"]["interval"] == "1m"
    assert experiment.outcomes["holdout"]["name"] == "untouched_holdout"
    assert experiment.outcomes["holdout"]["used_for_selection"] is False
    assert experiment.outcomes["orders_authorized"] is False
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
    with pytest.raises(RuntimeError, match="no symbols were evaluated"):
        handler({
            "scope": "strategy_research", "market_type": "FUTURES",
            "venue": "BINANCE_USDM_TESTNET",
        }, "job-3")
    assert memory.items == []
