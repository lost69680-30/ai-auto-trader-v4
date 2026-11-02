"""Conservative, research-only metrics for deterministic simulated trades.

Metrics are descriptive diagnostics. They do not rank strategies, tune thresholds,
forecast returns, or authorize paper/live orders.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

from backend.backtesting import BacktestResult, SimulatedTrade


@dataclass(frozen=True, slots=True)
class EvaluationMetrics:
    trade_count: int
    net_pnl: float
    total_costs: float
    expectancy_per_trade: float
    profit_factor: float | None
    win_rate_pct: float
    average_win: float
    average_loss_abs: float
    max_consecutive_losses: int
    orders_authorized: bool = False


@dataclass(frozen=True, slots=True)
class RiskAdjustedMetrics:
    net_return_pct: float
    max_drawdown_pct: float
    return_to_drawdown_ratio: float | None
    status: str
    orders_authorized: bool = False


def calculate_risk_adjusted_metrics(result: BacktestResult) -> RiskAdjustedMetrics:
    """Report net return relative to maximum drawdown; never infer a zero-risk ratio."""
    if not isinstance(result, BacktestResult) or result.orders_authorized is not False:
        raise ValueError("a paper-only BacktestResult is required")
    initial_equity = _finite_metric(result.initial_equity, "initial_equity")
    net_pnl = _finite_metric(result.net_pnl, "net_pnl")
    drawdown = _finite_metric(result.max_drawdown_pct, "max_drawdown_pct")
    if initial_equity <= 0:
        raise ValueError("initial_equity must be positive")
    if not 0 <= drawdown <= 100:
        raise ValueError("max_drawdown_pct must be between 0 and 100")
    net_return = net_pnl / initial_equity * 100.0
    if not math.isfinite(net_return):
        raise ValueError("net return percentage is not finite")
    if drawdown == 0:
        ratio = None
        status = "ZERO_DRAWDOWN_UNDEFINED"
    else:
        ratio = net_return / drawdown
        if not math.isfinite(ratio):
            raise ValueError("return-to-drawdown ratio is not finite")
        ratio = round(ratio, 8)
        status = "DEFINED"
    return RiskAdjustedMetrics(
        net_return_pct=round(net_return, 8),
        max_drawdown_pct=round(drawdown, 8),
        return_to_drawdown_ratio=ratio,
        status=status,
        orders_authorized=False,
    )


def _finite_metric(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite numeric data")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite numeric data")
    return result


def calculate_evaluation_metrics(
    trades: Iterable[SimulatedTrade],
) -> EvaluationMetrics:
    """Summarize already-simulated trades without changing their outcomes.

    Profit factor is undefined (None), rather than infinity, when there are
    no losing trades. Break-even trades count in the win-rate denominator and
    reset the consecutive-loss counter.
    """
    rows = tuple(trades)
    net_values: list[float] = []
    costs_values: list[float] = []

    for index, trade in enumerate(rows):
        if not isinstance(trade, SimulatedTrade):
            raise ValueError(f"trade at index {index} has an invalid type")
        net_values.append(_finite_metric(trade.net_pnl, f"trade {index} net_pnl"))
        costs = _finite_metric(trade.costs, f"trade {index} costs")
        if costs < 0:
            raise ValueError(f"trade {index} cost cannot be negative")
        costs_values.append(costs)

    count = len(net_values)
    total_net = math.fsum(net_values)
    total_costs = math.fsum(costs_values)
    winners = [value for value in net_values if value > 0]
    losers = [value for value in net_values if value < 0]
    gross_wins = math.fsum(winners)
    gross_losses_abs = math.fsum(abs(value) for value in losers)

    profit_factor = (
        gross_wins / gross_losses_abs if gross_losses_abs > 0 else None
    )
    max_loss_streak = 0
    current_loss_streak = 0
    for value in net_values:
        if value < 0:
            current_loss_streak += 1
            max_loss_streak = max(max_loss_streak, current_loss_streak)
        else:
            current_loss_streak = 0

    return EvaluationMetrics(
        trade_count=count,
        net_pnl=round(total_net, 8),
        total_costs=round(total_costs, 8),
        expectancy_per_trade=round(total_net / count, 8) if count else 0.0,
        profit_factor=round(profit_factor, 8) if profit_factor is not None else None,
        win_rate_pct=round(len(winners) / count * 100.0, 4) if count else 0.0,
        average_win=round(gross_wins / len(winners), 8) if winners else 0.0,
        average_loss_abs=round(gross_losses_abs / len(losers), 8) if losers else 0.0,
        max_consecutive_losses=max_loss_streak,
        orders_authorized=False,
    )
