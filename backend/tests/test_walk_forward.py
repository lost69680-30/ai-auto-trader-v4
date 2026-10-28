from datetime import datetime, timedelta, timezone

import pytest

from backend.strategy_research import Candle
from backend.walk_forward import WalkForwardConfig, evaluate_walk_forward

NOW = datetime(2026, 10, 10, 7, 0, tzinfo=timezone.utc)


def make_candles(n=240, *, drift=0.05):
    start = NOW - timedelta(minutes=n - 1)
    rows = []
    price = 100.0
    for i in range(n):
        price *= 1.0 + drift / 100.0
        rows.append(Candle("BTCUSDT", start + timedelta(minutes=i), price, price * 1.002,
                           price * 0.998, price, 1000.0))
    return rows


def test_walk_forward_has_chronological_disjoint_folds_and_untouched_holdout():
    rows = make_candles(360)
    result = evaluate_walk_forward(
        "BTCUSDT", rows,
        config=WalkForwardConfig(train_bars=120, validation_bars=40, holdout_bars=40,
                                 step_bars=40, candle_interval_minutes=1),
        research_now=NOW,
    )
    assert result.orders_authorized is False
    assert result.holdout is not None
    assert result.holdout.start_index == len(rows) - 40
    assert all(fold.start_index < fold.end_index for fold in result.folds)
    assert all(a.end_index <= b.start_index for a, b in zip(result.folds, result.folds[1:]))
    assert result.holdout.start_index >= max(f.end_index for f in result.folds)
    assert result.holdout.used_for_selection is False


def test_walk_forward_reports_buy_hold_and_cash_baselines():
    result = evaluate_walk_forward(
        "BTCUSDT", make_candles(360),
        config=WalkForwardConfig(train_bars=120, validation_bars=40, holdout_bars=40,
                                 step_bars=40),
        research_now=NOW,
    )
    assert result.folds
    for fold in result.folds:
        assert fold.buy_hold_return_pct is not None
        assert fold.cash_return_pct == 0.0
        assert fold.strategy.orders_authorized is False
    assert result.holdout.buy_hold_return_pct is not None
    assert result.holdout.cash_return_pct == 0.0


def test_walk_forward_rejects_short_or_irregular_data_without_trades():
    rows = make_candles(199)
    with pytest.raises(ValueError, match="not enough"):
        evaluate_walk_forward("BTCUSDT", rows,
            config=WalkForwardConfig(train_bars=120, validation_bars=40, holdout_bars=40, step_bars=40),
            research_now=NOW)
    rows = make_candles(360)
    rows[200] = Candle(rows[200].symbol, rows[200].timestamp + timedelta(minutes=1),
                       rows[200].open, rows[200].high, rows[200].low, rows[200].close, rows[200].volume)
    with pytest.raises(ValueError, match="interval"):
        evaluate_walk_forward("BTCUSDT", rows,
            config=WalkForwardConfig(train_bars=120, validation_bars=40, holdout_bars=40, step_bars=40),
            research_now=NOW)


def test_walk_forward_rejects_holdout_smaller_than_minimum():
    with pytest.raises(ValueError, match="holdout"):
        WalkForwardConfig(train_bars=120, validation_bars=40, holdout_bars=5, step_bars=40)
