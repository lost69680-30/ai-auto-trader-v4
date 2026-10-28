from datetime import datetime, timezone

import pytest

from backend.liquidity_structure import (
    Candle,
    LiquidityConfig,
    MarketStructureInput,
    rank_opportunities,
)


NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


def candle(o, h, l, c, volume=100.0):
    return Candle(open=o, high=h, low=l, close=c, volume=volume)


def sample_input(**overrides):
    values = dict(
        symbol="BTCUSDT",
        candles=(
            candle(100, 105, 95, 101),
            candle(101, 106, 96, 102),
            candle(102, 104, 97, 100),
            candle(100, 103, 98, 101),
            candle(101, 102, 99, 100),
            candle(100, 101, 98, 100),
        ),
        timestamp=NOW,
        reference_level=100.0,
        previous_close=99.0,
        current_funding_rate=0.0008,
        previous_funding_rate=0.0002,
        volume_baseline=100.0,
        estimated_round_trip_cost_pct=0.08,
    )
    values.update(overrides)
    return MarketStructureInput(**values)


def test_detects_high_sweep_as_short_candidate_without_authorizing_trade():
    data = sample_input(candles=(
        candle(100, 102, 99, 101),
        candle(101, 103, 100, 102),
        candle(102, 104, 101, 103),
        candle(103, 106, 102, 104),
        candle(104, 107, 101, 102),
    ))
    results = rank_opportunities([data], now=NOW, config=LiquidityConfig(lookback_candles=2))
    assert results
    assert any("LIQUIDITY_SWEEP_HIGH" in item.patterns for item in results)
    sweep = next(item for item in results if "LIQUIDITY_SWEEP_HIGH" in item.patterns)
    assert sweep.direction == "SHORT"
    assert sweep.orders_authorized is False


def test_detects_breakout_only_when_close_and_volume_confirm():
    data = sample_input(candles=(
        candle(100, 102, 99, 101), candle(101, 103, 100, 102),
        candle(102, 104, 101, 103), candle(103, 106, 102, 105.5, 180),
    ), reference_level=104.0, previous_close=103.0, volume_baseline=100.0,
      current_funding_rate=0.0, previous_funding_rate=0.0)
    results = rank_opportunities([data], now=NOW, config=LiquidityConfig(lookback_candles=2))
    assert any("BREAKOUT_UP" in item.patterns for item in results)
    assert next(item for item in results if "BREAKOUT_UP" in item.patterns).direction == "LONG"


def test_reclaim_crossing_reference_level_is_detected():
    data = sample_input(
        candles=(candle(98, 100, 97, 99), candle(99, 100, 98, 99.5), candle(99.5, 101, 98, 100.5)),
        reference_level=100.0,
        previous_close=99.0,
        current_funding_rate=0.0,
        previous_funding_rate=0.0,
        volume_baseline=100.0,
    )
    results = rank_opportunities([data], now=NOW, config=LiquidityConfig(lookback_candles=2, minimum_score=20))
    assert any("RECLAIM_UP" in item.patterns for item in results)


def test_funding_step_up_adds_short_crowding_evidence_not_a_standalone_order():
    data = sample_input(
        candles=(candle(100, 101, 99, 100), candle(100, 101, 99, 100), candle(100, 101, 99, 100)),
        reference_level=110.0,
        previous_close=100.0,
        current_funding_rate=0.0010,
        previous_funding_rate=0.0002,
        volume_baseline=100.0,
    )
    results = rank_opportunities([data], now=NOW, config=LiquidityConfig(lookback_candles=2))
    assert results
    assert any("FUNDING_STEP_UP" in item.patterns for item in results)
    assert all(item.orders_authorized is False for item in results)


def test_rank_is_deterministic_and_costs_reduce_score():
    strong = sample_input(symbol="AAAUSDT", candles=(
        candle(100, 102, 99, 101), candle(101, 103, 100, 102),
        candle(102, 104, 101, 103), candle(103, 106, 102, 105.5, 180),
    ), reference_level=104.0, previous_close=103.0,
        current_funding_rate=0.0, previous_funding_rate=0.0)
    costly = sample_input(symbol="BBBUSDT", candles=strong.candles,
        reference_level=104.0, previous_close=103.0,
        current_funding_rate=0.0, previous_funding_rate=0.0,
        estimated_round_trip_cost_pct=0.5)
    first = rank_opportunities([strong, costly], now=NOW, config=LiquidityConfig(lookback_candles=2))
    second = rank_opportunities([costly, strong], now=NOW, config=LiquidityConfig(lookback_candles=2))
    assert [(x.symbol, x.score) for x in first] == [(x.symbol, x.score) for x in second]
    assert first[0].symbol == "AAAUSDT"


def test_rejects_invalid_or_stale_data_fail_closed():
    bad = sample_input(candles=(candle(100, 99, 101, 100),))
    assert rank_opportunities([bad], now=NOW, config=LiquidityConfig(lookback_candles=2)) == ()


def test_config_rejects_invalid_thresholds():
    with pytest.raises(ValueError):
        LiquidityConfig(minimum_score=101)


def test_duplicate_symbols_fail_closed():
    data = sample_input()
    assert rank_opportunities([data, data], now=NOW, config=LiquidityConfig(lookback_candles=2)) == ()
