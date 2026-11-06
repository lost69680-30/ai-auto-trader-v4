"""Validation-only cost sensitivity analysis for offline strategy research.

The report recomputes validation trade outcomes at higher multiples of the
already-recorded estimated costs. It never reads the untouched holdout and has
no strategy promotion, order, position, or risk-setting interface.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

from backend.backtesting import BacktestResult
from backend.walk_forward import WalkForwardResult


@dataclass(frozen=True, slots=True)
class CostStressScenario:
    cost_multiplier: float
    fold_count: int
    trade_count: int
    winning_trades: int
    win_rate_pct: float
    total_gross_pnl: float
    total_estimated_costs: float
    total_net_pnl: float
    mean_net_return_pct: float
    mean_excess_return_pct: float
    positive_fold_pct: float
    maximum_fold_drawdown_pct: float
    orders_authorized: bool = False
    holdout_used: bool = False


@dataclass(frozen=True, slots=True)
class CostSensitivityReport:
    symbol: str
    scenarios: tuple[CostStressScenario, ...]
    orders_authorized: bool = False
    holdout_used: bool = False


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


def _fold_cost_stress(fold, multiplier: float) -> tuple[tuple[float, float, float, int, int, float], float, float]:
    backtest = fold.strategy
    if not isinstance(backtest, BacktestResult) or backtest.orders_authorized is not False:
        raise ValueError("validation fold must contain a paper-only backtest result")
    initial_equity = _finite(backtest.initial_equity, "initial equity")
    if initial_equity <= 0:
        raise ValueError("validation fold initial equity must be positive")
    if isinstance(backtest.trade_count, bool) or not isinstance(backtest.trade_count, int):
        raise ValueError("validation fold trade count must be an integer")
    if backtest.trade_count < 0 or backtest.trade_count != len(backtest.trades):
        raise ValueError("validation fold trade count does not match its trade ledger")
    benchmark = _finite(fold.buy_hold_return_pct, "buy-and-hold benchmark")
    equity = initial_equity
    peak = initial_equity
    maximum_drawdown = 0.0
    gross_total = 0.0
    costs_total = 0.0
    net_total = 0.0
    winners = 0
    for trade in backtest.trades:
        if trade.symbol != backtest.symbol or trade.direction not in {"LONG", "SHORT"}:
            raise ValueError("validation trade identity is invalid")
        gross = _finite(trade.gross_pnl, "trade gross P&L")
        costs = _finite(trade.costs, "trade estimated costs")
        if costs < 0:
            raise ValueError("trade estimated costs cannot be negative")
        if trade.entry_time.tzinfo is None or trade.entry_time.utcoffset() is None:
            raise ValueError("trade entry timestamp must be timezone-aware")
        if trade.exit_time.tzinfo is None or trade.exit_time.utcoffset() is None:
            raise ValueError("trade exit timestamp must be timezone-aware")
        if trade.exit_time <= trade.entry_time:
            raise ValueError("trade exit must be after trade entry")
        stressed_costs = costs * multiplier
        net = gross - stressed_costs
        if not math.isfinite(stressed_costs) or not math.isfinite(net):
            raise ValueError("stressed trade P&L or costs are non-finite")
        gross_total += gross
        costs_total += stressed_costs
        net_total += net
        winners += int(net > 0)
        executed_trades += 1
        equity = max(0.0, equity + net)
        peak = max(peak, equity)
        if peak > 0:
            maximum_drawdown = max(maximum_drawdown, (peak - equity) / peak * 100.0)
        if equity <= 0:
            break
    if not all(math.isfinite(value) for value in (gross_total, costs_total, net_total, maximum_drawdown)):
        raise ValueError("validation fold aggregate metrics are non-finite")
    net_return_pct = net_total / initial_equity * 100.0
    return (
        gross_total,
        costs_total,
        net_total,
        winners,
        executed_trades,
        max(maximum_drawdown, _finite(backtest.max_drawdown_pct, "base drawdown")),
    ), net_return_pct, benchmark


def summarize_validation_cost_sensitivity(
    result: WalkForwardResult,
    *,
    cost_multipliers: Iterable[float] = (1.0, 1.5, 2.0),
) -> CostSensitivityReport:
    """Recompute validation-fold returns using higher multiples of estimated costs.

    Cost multipliers must be monotonically increasing and at least 1.0. All
    calculations use only WalkForwardResult.folds; the untouched final holdout
    is intentionally never read.
    """
    if not isinstance(result, WalkForwardResult) or result.orders_authorized is not False:
        raise ValueError("a paper-only WalkForwardResult is required")
    folds = result.folds
    if not isinstance(folds, tuple) or not folds:
        raise ValueError("at least one validation folds is required")
    multipliers_raw = tuple(cost_multipliers)
    if not multipliers_raw or len(multipliers_raw) > 10:
        raise ValueError("one to ten cost multipliers are required")
    multipliers = tuple(_finite(value, "cost multiplier") for value in multipliers_raw)
    if any(value < 1.0 for value in multipliers):
        raise ValueError("cost multipliers must be at least 1.0")
    if any(right <= left for left, right in zip(multipliers, multipliers[1:])):
        raise ValueError("cost multipliers must be strictly increasing")

    scenarios: list[CostStressScenario] = []
    for multiplier in multipliers:
        gross_values: list[float] = []
        cost_values: list[float] = []
        net_values: list[float] = []
        returns: list[float] = []
        excess_returns: list[float] = []
        drawdowns: list[float] = []
        winners = 0
        trades = 0
        for fold in folds:
            (gross, costs, net, fold_winners, fold_trades, drawdown), net_return_pct, benchmark = (
                _fold_cost_stress(fold, multiplier)
            )
            gross_values.append(gross)
            cost_values.append(costs)
            net_values.append(net)
            returns.append(net_return_pct)
            excess_returns.append(net_return_pct - benchmark)
            drawdowns.append(drawdown)
            winners += fold_winners
            trades += fold_trades
        total_gross = math.fsum(gross_values)
        total_costs = math.fsum(cost_values)
        total_net = math.fsum(net_values)
        mean_return = math.fsum(returns) / len(returns)
        mean_excess = math.fsum(excess_returns) / len(excess_returns)
        positive_fold_pct = sum(value > 0 for value in excess_returns) / len(excess_returns) * 100.0
        scenario = CostStressScenario(
            cost_multiplier=multiplier,
            fold_count=len(folds),
            trade_count=trades,
            winning_trades=winners,
            win_rate_pct=round(winners / trades * 100.0, 4) if trades else 0.0,
            total_gross_pnl=round(total_gross, 8),
            total_estimated_costs=round(total_costs, 8),
            total_net_pnl=round(total_net, 8),
            mean_net_return_pct=round(mean_return, 6),
            mean_excess_return_pct=round(mean_excess, 6),
            positive_fold_pct=round(positive_fold_pct, 4),
            maximum_fold_drawdown_pct=round(max(drawdowns, default=0.0), 4),
            orders_authorized=False,
            holdout_used=False,
        )
        scenarios.append(scenario)
    return CostSensitivityReport(
        symbol=result.symbol,
        scenarios=tuple(scenarios),
        orders_authorized=False,
        holdout_used=False,
    )
