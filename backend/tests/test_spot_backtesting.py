from datetime import datetime, timedelta, timezone

import pytest

from backend.strategy_research import Candle
from backend.spot_backtesting import (
    SpotBacktestConfig,
    evaluate_spot_walk_forward,
    run_spot_backtest,
)

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def candles(n=180, *, drift=0.05):
    start = NOW - timedelta(minutes=n - 1)
    rows = []
    price = 100.0
    for i in range(n):
        price *= 1 + drift / 100
        rows.append(Candle("BTCUSDT", start + timedelta(minutes=i), price,
                           price * 1.003, price * 0.997, price, 1000.0))
    return rows


def test_spot_backtest_is_long_only_costed_and_never_authorizes_orders():
    cfg = SpotBacktestConfig(candle_interval_minutes=1, min_candles=30)
    result = run_spot_backtest("BTCUSDT", candles(), config=cfg, research_now=NOW)
    assert result.orders_authorized is False
    assert result.trade_count > 0
    assert all(trade.direction == "LONG" for trade in result.trades)
    assert result.max_position_fraction <= 0.10
    assert result.funding_cost == 0.0
    assert result.total_costs > 0
    assert all(trade.costs > 0 for trade in result.trades)


def test_spot_backtest_rejects_gapped_data_and_negative_costs():
    rows = candles()
    row = rows[50]
    rows[50] = Candle(row.symbol, row.timestamp + timedelta(minutes=1),
                      row.open, row.high, row.low, row.close, row.volume)
    result = run_spot_backtest("BTCUSDT", rows, research_now=NOW)
    assert result.trade_count == 0
    assert any("interval" in warning.lower() for warning in result.warnings)
    with pytest.raises(ValueError, match="non-negative"):
        SpotBacktestConfig(fee_bps_per_side=-1)


def test_spot_walk_forward_reserves_holdout_and_compares_baselines():
    result = evaluate_spot_walk_forward(
        "BTCUSDT", candles(360),
        train_bars=120, validation_bars=40, holdout_bars=40, step_bars=40,
        config=SpotBacktestConfig(candle_interval_minutes=1, min_candles=30),
        research_now=NOW,
    )
    assert result.orders_authorized is False
    assert result.holdout.start_index == 320
    assert result.holdout.used_for_selection is False
    assert result.holdout.cash_return_pct == 0.0
    assert result.holdout.buy_hold_return_pct is not None
    assert all(window.end_index <= result.holdout.start_index for window in result.folds)
    assert all(window.strategy.orders_authorized is False for window in result.folds)
    assert all(window.strategy.trades[0].entry_time >= candles(360)[window.start_index].timestamp
               for window in result.folds if window.strategy.trades)


def test_walk_forward_rejects_short_history_and_future_candles():
    with pytest.raises(ValueError, match="not enough"):
        evaluate_spot_walk_forward(
            "BTCUSDT", candles(100), train_bars=40, validation_bars=40,
            holdout_bars=40, step_bars=20, research_now=NOW,
        )
    rows = candles(360)
    row = rows[-1]
    rows[-1] = Candle(row.symbol, NOW + timedelta(minutes=1), row.open, row.high, row.low, row.close, row.volume)
    with pytest.raises(ValueError, match="future"):
        evaluate_spot_walk_forward(
            "BTCUSDT", rows, train_bars=120, validation_bars=40,
            holdout_bars=40, step_bars=40, research_now=NOW,
        )


def test_insufficient_history_is_warning_not_a_profit_claim():
    result = run_spot_backtest("BTCUSDT", candles(20), research_now=NOW)
    assert result.trade_count == 0
    assert result.orders_authorized is False
    assert any("insufficient history" in warning.lower() for warning in result.warnings)


def test_walk_forward_rejects_overlapping_validation_windows():
    with pytest.raises(ValueError, match="disjoint"):
        evaluate_spot_walk_forward(
            "BTCUSDT", candles(360), train_bars=120, validation_bars=40,
            holdout_bars=40, step_bars=20, research_now=NOW,
        )
