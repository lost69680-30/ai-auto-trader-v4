from datetime import datetime, timedelta, timezone

import pytest

from backend.backtesting import BacktestConfig, run_backtest
from backend.strategy_research import Candle

NOW = datetime(2026, 10, 10, 7, 0, tzinfo=timezone.utc)


def candles(closes, *, step_minutes=1):
    start = NOW - timedelta(minutes=(len(closes) - 1) * step_minutes)
    result = []
    for i, close in enumerate(closes):
        result.append(Candle(
            symbol="BTCUSDT", timestamp=start + timedelta(minutes=i * step_minutes),
            open=close, high=close * 1.002, low=close * 0.998,
            close=close, volume=1000.0,
        ))
    return result


def test_backtest_is_reproducible_and_never_authorizes_orders():
    rows = candles([100 + i for i in range(50)])
    first = run_backtest("BTCUSDT", rows, config=BacktestConfig(), research_now=NOW)
    second = run_backtest("BTCUSDT", rows, config=BacktestConfig(), research_now=NOW)
    assert first == second
    assert first.orders_authorized is False
    assert first.trades
    assert first.trade_count == len(first.trades)


def test_spot_backtest_default_does_not_charge_futures_funding():
    # Public Spot OHLCV has no funding payments; those belong to Futures-specific
    # evaluation and must not silently reduce Spot strategy results.
    assert BacktestConfig().funding_bps_per_8h == 0.0


def test_costs_reduce_net_pnl_for_same_price_path():
    rows = candles([100 + i * 0.8 for i in range(50)])
    free = run_backtest("BTCUSDT", rows,
        config=BacktestConfig(fee_bps_per_side=0, slippage_bps_per_side=0, funding_bps_per_8h=0),
        research_now=NOW)
    costly = run_backtest("BTCUSDT", rows,
        config=BacktestConfig(fee_bps_per_side=5, slippage_bps_per_side=3, funding_bps_per_8h=2),
        research_now=NOW)
    assert costly.net_pnl <= free.net_pnl
    assert costly.total_costs > free.total_costs


def test_flat_market_does_not_create_trades():
    result = run_backtest("BTCUSDT", candles([100.0] * 50), research_now=NOW)
    assert result.trade_count == 0
    assert result.net_pnl == 0
    assert result.max_drawdown_pct == 0


def test_invalid_or_short_history_fails_closed():
    result = run_backtest("BTCUSDT", candles([100 + i for i in range(10)]), research_now=NOW)
    assert result.trade_count == 0
    assert result.warnings
    with pytest.raises(ValueError):
        BacktestConfig(position_fraction=1.5)
    with pytest.raises(ValueError):
        run_backtest("BTCUSDT", [], research_now=datetime(2026, 10, 10))


def test_metrics_are_finite_and_losses_are_accounted_for():
    rows = candles([100 + i for i in range(35)] + [134 - i * 1.4 for i in range(15)])
    result = run_backtest("BTCUSDT", rows, research_now=NOW)
    assert result.trade_count == len(result.trades)
    assert 0 <= result.win_rate_pct <= 100
    assert result.max_drawdown_pct >= 0
    assert result.ending_equity >= 0
    assert result.orders_authorized is False


def test_backtest_rejects_missing_candle_intervals_fail_closed():
    rows = candles([100 + i * 0.4 for i in range(60)])
    del rows[25]  # A missing bar preserves timestamp order but creates a 2-minute gap.
    result = run_backtest("BTCUSDT", rows, research_now=NOW)
    assert result.trade_count == 0
    assert result.trades == ()
    assert result.ending_equity == result.initial_equity
    assert any("gap" in warning.lower() or "interval" in warning.lower() for warning in result.warnings)
    assert result.orders_authorized is False


def test_backtest_enforces_configured_candle_interval():
    rows = candles([100 + i * 0.6 for i in range(60)], step_minutes=1)
    result = run_backtest(
        "BTCUSDT", rows, config=BacktestConfig(candle_interval_minutes=5), research_now=NOW,
    )
    assert result.trade_count == 0
    assert result.trades == ()
    assert result.ending_equity == result.initial_equity
    assert any("interval" in warning.lower() for warning in result.warnings)
    assert result.orders_authorized is False


def test_backtest_accepts_contiguous_non_minute_interval():
    rows = candles([100 + i * 0.3 for i in range(60)], step_minutes=5)
    result = run_backtest(
        "BTCUSDT", rows, config=BacktestConfig(candle_interval_minutes=5), research_now=NOW,
    )
    assert not any("gap" in warning.lower() or "interval" in warning.lower() for warning in result.warnings)
    assert result.orders_authorized is False


def test_backtest_start_timestamp_uses_prior_candles_only_as_warmup():
    rows = candles([100 + i * 0.8 for i in range(100)])
    start = rows[60].timestamp
    result = run_backtest("BTCUSDT", rows, research_now=NOW, start_timestamp=start)
    assert all(trade.entry_time >= start for trade in result.trades)
    assert result.orders_authorized is False
