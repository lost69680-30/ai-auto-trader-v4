from datetime import datetime, timedelta, timezone

import pytest

from backend.backtesting import BacktestResult, SimulatedTrade
from backend.cost_sensitivity import summarize_validation_cost_sensitivity
from backend.walk_forward import EvaluationWindow, MarketRegimeEvidence, WalkForwardResult


UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)


def _trade(*, gross: float, costs: float, minutes: int = 1):
    return SimulatedTrade(
        symbol="BTCUSDT",
        direction="LONG",
        entry_time=START,
        exit_time=START + timedelta(minutes=minutes),
        entry_price=100.0,
        exit_price=101.0,
        notional=100.0,
        gross_pnl=gross,
        costs=costs,
        net_pnl=gross - costs,
        strategy_consensus="LONG",
    )


def _window(name: str, *, gross: float, costs: float):
    trade = _trade(gross=gross, costs=costs)
    backtest = BacktestResult(
        symbol="BTCUSDT",
        initial_equity=1000.0,
        ending_equity=1000.0 + gross - costs,
        net_pnl=gross - costs,
        total_costs=costs,
        trade_count=1,
        wins=1 if gross - costs > 0 else 0,
        losses=1 if gross - costs < 0 else 0,
        win_rate_pct=100.0 if gross - costs > 0 else 0.0,
        max_drawdown_pct=0.0,
        trades=(trade,),
        warnings=(),
        orders_authorized=False,
    )
    regime = MarketRegimeEvidence(
        trend_state="UP",
        volatility_state="LOW",
        trend_return_pct=1.0,
        realized_volatility_pct=0.1,
        observations=10,
        methodology="test fixture",
        orders_authorized=False,
    )
    return EvaluationWindow(
        name=name,
        start_index=0,
        end_index=10,
        strategy=backtest,
        market_regime=regime,
        buy_hold_return_pct=0.1,
        used_for_selection=False,
    )


def _result():
    first = _window("validation_1", gross=12.0, costs=2.0)
    second = _window("validation_2", gross=8.0, costs=2.0)
    holdout = _window("untouched_holdout", gross=999999.0, costs=0.0)
    return WalkForwardResult(
        symbol="BTCUSDT",
        folds=(first, second),
        holdout=holdout,
        warnings=(),
        orders_authorized=False,
    )


def test_cost_sensitivity_returns_baseline_and_stress_cases_only_for_validation_folds():
    report = summarize_validation_cost_sensitivity(_result())

    assert [row.cost_multiplier for row in report.scenarios] == [1.0, 1.5, 2.0]
    assert [row.trade_count for row in report.scenarios] == [2, 2, 2]
    assert [row.total_estimated_costs for row in report.scenarios] == [4.0, 6.0, 8.0]
    assert [row.total_net_pnl for row in report.scenarios] == [16.0, 14.0, 12.0]
    assert all(row.fold_count == 2 for row in report.scenarios)
    assert report.orders_authorized is False


def test_cost_stress_never_improves_net_return_as_assumed_costs_increase():
    report = summarize_validation_cost_sensitivity(_result())
    returns = [row.mean_net_return_pct for row in report.scenarios]
    assert returns[0] >= returns[1] >= returns[2]


def test_cost_sensitivity_never_uses_untouched_holdout():
    first = _result()
    changed_holdout = _window("untouched_holdout", gross=-999999.0, costs=999999.0)
    second = WalkForwardResult(
        symbol=first.symbol,
        folds=first.folds,
        holdout=changed_holdout,
        warnings=(),
        orders_authorized=False,
    )
    assert summarize_validation_cost_sensitivity(first) == summarize_validation_cost_sensitivity(second)


def test_cost_sensitivity_rejects_unsafe_walk_forward_result():
    result = _result()
    unsafe = WalkForwardResult(
        symbol=result.symbol,
        folds=result.folds,
        holdout=result.holdout,
        warnings=(),
        orders_authorized=True,
    )
    with pytest.raises(ValueError, match="paper-only"):
        summarize_validation_cost_sensitivity(unsafe)


def test_cost_sensitivity_rejects_empty_validation_fold_history():
    result = _result()
    empty = WalkForwardResult(
        symbol=result.symbol,
        folds=(),
        holdout=result.holdout,
        warnings=(),
        orders_authorized=False,
    )
    with pytest.raises(ValueError, match="validation folds"):
        summarize_validation_cost_sensitivity(empty)


def test_cost_sensitivity_rejects_invalid_trade_costs():
    result = _result()
    bad_trade = _trade(gross=1.0, costs=-1.0)
    bad_backtest = BacktestResult(
        symbol="BTCUSDT", initial_equity=1000.0, ending_equity=1002.0,
        net_pnl=2.0, total_costs=-1.0, trade_count=1, wins=1, losses=0,
        win_rate_pct=100.0, max_drawdown_pct=0.0, trades=(bad_trade,),
        warnings=(), orders_authorized=False,
    )
    original = result.folds[0]
    bad_fold = EvaluationWindow(
        name=original.name, start_index=original.start_index, end_index=original.end_index,
        strategy=bad_backtest, market_regime=original.market_regime,
        buy_hold_return_pct=original.buy_hold_return_pct, used_for_selection=False,
    )
    invalid = WalkForwardResult(
        symbol=result.symbol, folds=(bad_fold,), holdout=result.holdout,
        warnings=(), orders_authorized=False,
    )
    with pytest.raises(ValueError, match="cost"):
        summarize_validation_cost_sensitivity(invalid)


def test_cost_stress_stops_simulating_after_stressed_equity_is_exhausted():
    first = _trade(gross=-1000.0, costs=1.0)
    second = _trade(gross=500.0, costs=1.0, minutes=2)
    backtest = BacktestResult(
        symbol="BTCUSDT",
        initial_equity=1000.0,
        ending_equity=499.0,
        net_pnl=-501.0,
        total_costs=2.0,
        trade_count=2,
        wins=1,
        losses=1,
        win_rate_pct=50.0,
        max_drawdown_pct=50.1,
        trades=(first, second),
        warnings=(),
        orders_authorized=False,
    )
    base = _result()
    original = base.folds[0]
    exhausted = EvaluationWindow(
        name=original.name,
        start_index=original.start_index,
        end_index=original.end_index,
        strategy=backtest,
        market_regime=original.market_regime,
        buy_hold_return_pct=original.buy_hold_return_pct,
        used_for_selection=False,
    )
    result = WalkForwardResult(
        symbol="BTCUSDT",
        folds=(exhausted,),
        holdout=base.holdout,
        warnings=(),
        orders_authorized=False,
    )

    scenario = summarize_validation_cost_sensitivity(result, cost_multipliers=(2.0,)).scenarios[0]
    assert scenario.trade_count == 1
    assert scenario.total_net_pnl == -1002.0
