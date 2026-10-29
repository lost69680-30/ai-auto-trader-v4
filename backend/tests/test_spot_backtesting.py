from datetime import datetime, timedelta, timezone

import pytest

from backend.strategy_research import Candle, ResearchConfig
from backend.spot_backtesting import SpotBacktestConfig, run_spot_backtest, evaluate_spot_walk_forward

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


def test_spot_backtest_is_long_only_and_never_authorizes_orders():
    cfg = SpotBacktestConfig(candle_interval_minutes=1, min_candles=30)
    result = run_spot_backtest("BTCUSDT", candles(), config=cfg, research_now=NOW)
    assert result.orders_authorized is False
    assert all(trade.direction == "LONG" for trade in result.trades)
    assert result.max_position_fraction <= 0.10
    assert result.funding_cost == 0.0


def test_spot_backtest_validates_gaps_and_invalid_costs_fail_closed():
    rows = candles()
    rows[50] = Candle(rows[50].symbol, rows[50].timestamp + timedelta(minutes=1),
                      rows[50].open, rows[50].high, rows[50].low, rows[50].close, rows[50].volume)
    result = run_spot_backtest("BTCUSDT", rows, research_now=NOW)
    assert result.trade_count == 0
    assert any("interval" in warning.lower() for warning in result.warnings)
    with pytest.raises(ValueError, match="non-negative"):
        SpotBacktestConfig(fee_bps_per_side=-1)


def test_spot_walk_forward_reserves_untouched_holdout_and_compares_baselines():
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


def test_spot_walk_forward_rejects_short_history_and_future_leakage_boundaries():
    with pytest.raises(ValueError, match="not enough"):
        evaluate_spot_walk_forward(
            "BTCUSDT", candles(100), train_bars=40, validation_bars=40,
            holdout_bars=40, step_bars=20, research_now=NOW,
        )
    rows = candles(360)
    rows[-1] = Candle(rows[-1].symbol, rows[-1].timestamp + timedelta(minutes=1),
                      rows[-1].open, rows[-1].high, rows[-1].low, rows[-1].close, rows[-1].volume)
    with pytest.raises(ValueError, match="future"):
        evaluate_spot_walk_forward(
            "BTCUSDT", rows, train_bars=120, validation_bars=40,
            holdout_bars=40, step_bars=40, research_now=NOW,
        )
