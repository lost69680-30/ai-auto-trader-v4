from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from backend.backtesting import SimulatedTrade
from backend.evaluation_metrics import calculate_evaluation_metrics


def trade(net_pnl, costs=1.0):
    stamp = datetime(2026, 11, 1, tzinfo=timezone.utc)
    return SimulatedTrade(
        symbol="BTCUSDT",
        direction="LONG",
        entry_time=stamp,
        exit_time=stamp,
        entry_price=100.0,
        exit_price=101.0,
        notional=100.0,
        gross_pnl=net_pnl + costs,
        costs=costs,
        net_pnl=net_pnl,
        strategy_consensus="REVIEW_LONG",
    )


def test_metrics_report_net_expectancy_profit_factor_and_loss_streak():
    result = calculate_evaluation_metrics([
        trade(4.0, costs=1.0),
        trade(-2.0, costs=1.0),
        trade(-3.0, costs=1.0),
        trade(1.0, costs=1.0),
        trade(0.0, costs=1.0),
    ])

    assert result.trade_count == 5
    assert result.net_pnl == 0.0
    assert result.total_costs == 5.0
    assert result.expectancy_per_trade == 0.0
    assert result.profit_factor == pytest.approx(1.0)
    assert result.win_rate_pct == 40.0
    assert result.average_win == 2.5
    assert result.average_loss_abs == 2.5
    assert result.max_consecutive_losses == 2
    assert result.orders_authorized is False


def test_metrics_empty_input_is_finite_and_has_no_infinite_profit_factor():
    result = calculate_evaluation_metrics([])

    assert result.trade_count == 0
    assert result.net_pnl == 0.0
    assert result.expectancy_per_trade == 0.0
    assert result.profit_factor is None
    assert result.win_rate_pct == 0.0
    assert result.max_consecutive_losses == 0
    assert result.orders_authorized is False


def test_metrics_all_winners_report_undefined_profit_factor_not_infinity():
    result = calculate_evaluation_metrics([trade(2.0), trade(3.0)])

    assert result.profit_factor is None
    assert result.average_loss_abs == 0.0
    assert result.average_win == 2.5


def test_metrics_reject_non_finite_or_negative_cost_inputs():
    with pytest.raises(ValueError, match="finite"):
        calculate_evaluation_metrics([trade(float("nan"))])
    with pytest.raises(ValueError, match="cost"):
        calculate_evaluation_metrics([trade(1.0, costs=-1.0)])


def test_metrics_reject_aggregate_overflow_instead_of_returning_non_finite_values():
    with pytest.raises(ValueError, match="aggregate metrics"):
        calculate_evaluation_metrics([trade(1e308, costs=0.0), trade(1e308, costs=0.0)])


def test_metrics_reject_profit_factor_overflow():
    with pytest.raises(ValueError, match="profit factor"):
        calculate_evaluation_metrics([trade(1e308, costs=0.0), trade(-1e-308, costs=0.0)])
