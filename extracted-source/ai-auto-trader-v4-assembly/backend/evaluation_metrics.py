"""Conservative, research-only metrics for deterministic simulated trades.

Metrics are descriptive diagnostics. They do not rank strategies, tune thresholds,
forecast returns, or authorize paper/live orders.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

from backend.backtesting import SimulatedTrade


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


def _finite_metric(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite numeric data")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite numeric data")
    return result


def _finite_sum(values: Iterable[float], name: str) -> float:
    try:
        result = math.fsum(values)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} aggregate metrics are not finite") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} aggregate metrics are not finite")
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
    total_net = _finite_sum(net_values, "net PnL")
    total_costs = _finite_sum(costs_values, "cost")
    winners = [value for value in net_values if value > 0]
    losers = [value for value in net_values if value < 0]
    gross_wins = _finite_sum(winners, "gross wins")
    gross_losses_abs = _finite_sum((abs(value) for value in losers), "gross losses")

    profit_factor = None
    if gross_losses_abs > 0:
        profit_factor = gross_wins / gross_losses_abs
        if not math.isfinite(profit_factor):
            raise ValueError("profit factor is not finite")
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