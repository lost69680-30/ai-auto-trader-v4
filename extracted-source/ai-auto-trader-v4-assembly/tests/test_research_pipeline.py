from datetime import datetime, timezone

from app.engines.research_pipeline import analyze_research_universe
from backend.scanner import Contract, MarketSnapshot

NOW = datetime(2026, 10, 10, 7, 0, tzinfo=timezone.utc)


def test_scanner_candidate_without_complete_downstream_evidence_fails_closed():
    result = analyze_research_universe(
        contracts=[Contract("BTCUSDT", "TRADING", "PERPETUAL", "USDT")],
        market_snapshots=[MarketSnapshot("BTCUSDT", NOW, 50000, 5_000_000, 3, 1.5)],
        candles_by_symbol={},
        structure_inputs=[],
        now=NOW,
    )

    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.symbol == "BTCUSDT"
    assert candidate.status == "NO_TRADE"
    assert "candle history" in " ".join(candidate.reasons).lower()
    assert result.orders_authorized is False
    assert candidate.orders_authorized is False


def test_stale_scanner_snapshot_produces_no_candidate():
    result = analyze_research_universe(
        contracts=[Contract("BTCUSDT", "TRADING", "PERPETUAL", "USDT")],
        market_snapshots=[MarketSnapshot("BTCUSDT", NOW, 50000, 5_000_000, 3, 1.5)],
        candles_by_symbol={},
        structure_inputs=[],
        now=NOW.replace(minute=10),
    )

    assert result.candidates == ()
    assert result.rejections
    assert result.orders_authorized is False


def test_candidate_runs_opportunity_and_strategy_engines_but_missing_liquidity_means_no_trade():
    from datetime import timedelta
    from backend.strategy_research import Candle

    candles = tuple(
        Candle(
            symbol="BTCUSDT",
            timestamp=NOW - timedelta(minutes=34 - index),
            open=100 + index,
            high=101 + index,
            low=99 + index,
            close=100 + index,
            volume=1000 + index * 10,
        )
        for index in range(35)
    )
    result = analyze_research_universe(
        contracts=[Contract("BTCUSDT", "TRADING", "PERPETUAL", "USDT")],
        market_snapshots=[MarketSnapshot("BTCUSDT", NOW, 134, 5_000_000, 3, 1.5)],
        candles_by_symbol={"BTCUSDT": candles},
        structure_inputs=[],
        now=NOW,
    )

    candidate = result.candidates[0]
    assert candidate.opportunity is not None
    assert candidate.strategy_research is not None
    assert candidate.liquidity is None
    assert candidate.status == "NO_TRADE"
    assert candidate.direction == "NONE"
    assert candidate.orders_authorized is False
