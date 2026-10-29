"""Offline Spot-only backtest and walk-forward evaluation; never places orders."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Iterable

from backend.strategy_research import Candle


@dataclass(frozen=True, slots=True)
class SpotBacktestConfig:
    initial_equity: float = 10_000.0
    position_fraction: float = 0.10
    fee_bps_per_side: float = 10.0
    slippage_bps_per_side: float = 5.0
    candle_interval_minutes: int = 1
    min_candles: int = 30
    moving_average_period: int = 20

    def __post_init__(self) -> None:
        values = (self.initial_equity, self.position_fraction, self.fee_bps_per_side,
                  self.slippage_bps_per_side)
        if not all(math.isfinite(v) for v in values):
            raise ValueError("configuration values must be finite")
        if self.initial_equity <= 0 or not 0 < self.position_fraction <= 0.10:
            raise ValueError("Spot position fraction must be positive and at most 10%")
        if self.fee_bps_per_side < 0 or self.slippage_bps_per_side < 0:
            raise ValueError("cost assumptions must be non-negative")
        if self.candle_interval_minutes <= 0 or self.min_candles < 3 or self.moving_average_period < 2:
            raise ValueError("invalid cadence or lookback")


@dataclass(frozen=True, slots=True)
class SpotTrade:
    direction: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    net_pnl: float
    costs: float


@dataclass(frozen=True, slots=True)
class SpotBacktestResult:
    symbol: str
    initial_equity: float
    ending_equity: float
    net_return_pct: float
    trade_count: int
    max_drawdown_pct: float
    total_costs: float
    trades: tuple[SpotTrade, ...]
    warnings: tuple[str, ...]
    max_position_fraction: float
    funding_cost: float = 0.0
    orders_authorized: bool = False


@dataclass(frozen=True, slots=True)
class SpotEvaluationWindow:
    name: str
    start_index: int
    end_index: int
    strategy: SpotBacktestResult
    buy_hold_return_pct: float
    cash_return_pct: float = 0.0
    used_for_selection: bool = False


@dataclass(frozen=True, slots=True)
class SpotWalkForwardResult:
    symbol: str
    folds: tuple[SpotEvaluationWindow, ...]
    holdout: SpotEvaluationWindow
    warnings: tuple[str, ...]
    orders_authorized: bool = False


def _validate(symbol: str, rows: list[Candle], cfg: SpotBacktestConfig, now: datetime) -> str | None:
    if not symbol or not rows:
        return "symbol and candle data are required"
    if now.tzinfo is None or now.utcoffset() is None:
        return "evaluation clock must be timezone-aware"
    previous = None
    expected = timedelta(minutes=cfg.candle_interval_minutes)
    now_utc = now.astimezone(timezone.utc)
    for i, row in enumerate(rows):
        if row.symbol != symbol:
            return f"mixed symbol at row {i}"
        if row.timestamp.tzinfo is None or row.timestamp.utcoffset() is None:
            return f"timestamp at row {i} must be timezone-aware"
        stamp = row.timestamp.astimezone(timezone.utc)
        if stamp > now_utc:
            return f"future candle timestamp at row {i}"
        if previous is not None and stamp - previous != expected:
            return f"candle interval gap or mismatch at row {i}"
        values = (row.open, row.high, row.low, row.close, row.volume)
        if not all(math.isfinite(v) for v in values):
            return f"non-finite OHLCV at row {i}"
        if min(row.open, row.high, row.low, row.close) <= 0 or row.volume < 0:
            return f"invalid OHLCV at row {i}"
        if row.high < max(row.open, row.close, row.low) or row.low > min(row.open, row.close, row.high):
            return f"inconsistent OHLC at row {i}"
        previous = stamp
    return None


def _simulate_window(symbol: str, rows: list[Candle], start: int, end: int,
                     cfg: SpotBacktestConfig) -> SpotBacktestResult:
    equity = cfg.initial_equity
    equity_points = [equity]
    trades: list[SpotTrade] = []
    total_costs = 0.0
    cost_rate = 2.0 * (cfg.fee_bps_per_side + cfg.slippage_bps_per_side) / 10_000.0
    # Indicators may read prior training candles; entries/exits must remain inside the OOS window.
    i = max(start, cfg.moving_average_period)
    while i + 1 < end and equity > 0:
        history = rows[i - cfg.moving_average_period:i]
        mean = sum(c.close for c in history) / cfg.moving_average_period
        # Signal is known after candle i-1 closes; entry is at candle i open, exit at i+1 open.
        if rows[i - 1].close > mean:
            notional = equity * cfg.position_fraction
            entry, exit_row = rows[i], rows[i + 1]
            gross = notional * (exit_row.open / entry.open - 1.0)
            costs = notional * cost_rate
            net = gross - costs
            trades.append(SpotTrade("LONG", entry.timestamp, exit_row.timestamp,
                                    entry.open, exit_row.open, net, costs))
            equity = max(0.0, equity + net)
            total_costs += costs
            equity_points.append(equity)
            i += 2
        else:
            i += 1
    peak = equity_points[0]
    drawdown = 0.0
    for point in equity_points:
        peak = max(peak, point)
        if peak > 0:
            drawdown = max(drawdown, (peak - point) / peak * 100.0)
    warnings = [
        "Research simulation only; fee and slippage inputs are assumptions, not verified live costs.",
        "Long-only Spot; no shorts, leverage, Futures mark price, funding, or order execution.",
    ]
    if len(trades) < 30:
        warnings.append("Insufficient trade sample (<30); performance estimates are highly uncertain.")
    return SpotBacktestResult(
        symbol, cfg.initial_equity, equity, (equity / cfg.initial_equity - 1.0) * 100.0,
        len(trades), drawdown, total_costs, tuple(trades), tuple(warnings),
        cfg.position_fraction, 0.0, False,
    )


def run_spot_backtest(symbol: str, candles: Iterable[Candle], *,
                      config: SpotBacktestConfig | None = None,
                      research_now: datetime) -> SpotBacktestResult:
    """Evaluate a fixed moving-average rule on the supplied full interval."""
    cfg = config or SpotBacktestConfig()
    rows = list(candles)
    invalid = _validate(symbol, rows, cfg, research_now)
    if invalid:
        return SpotBacktestResult(symbol, cfg.initial_equity, cfg.initial_equity, 0.0, 0,
                                  0.0, 0.0, (), (f"Input rejected: {invalid}",),
                                  cfg.position_fraction, 0.0, False)
    if len(rows) < max(cfg.min_candles, cfg.moving_average_period + 2):
        return SpotBacktestResult(symbol, cfg.initial_equity, cfg.initial_equity, 0.0, 0,
                                  0.0, 0.0, (), ("Insufficient history; no performance conclusion.",),
                                  cfg.position_fraction, 0.0, False)
    return _simulate_window(symbol, rows, cfg.moving_average_period, len(rows), cfg)


def _buy_hold_return(segment: list[Candle], cfg: SpotBacktestConfig) -> float:
    if len(segment) < 2:
        return 0.0
    gross_return = segment[-1].close / segment[0].open - 1.0
    # Same maximum allocation and same round-trip cost assumptions as the strategy.
    net_equity_return = cfg.position_fraction * (gross_return -
        2.0 * (cfg.fee_bps_per_side + cfg.slippage_bps_per_side) / 10_000.0)
    return net_equity_return * 100.0


def evaluate_spot_walk_forward(symbol: str, candles: Iterable[Candle], *,
                               train_bars: int = 2000, validation_bars: int = 500,
                               holdout_bars: int = 500, step_bars: int = 500,
                               config: SpotBacktestConfig | None = None,
                               research_now: datetime) -> SpotWalkForwardResult:
    """Run fixed settings on chronological OOS folds and one reserved final holdout.

    No parameter tuning occurs. Training rows supply indicator warmup only; simulated trades
    and benchmark returns are restricted to each out-of-sample segment.
    """
    cfg = config or SpotBacktestConfig()
    rows = list(candles)
    if min(train_bars, validation_bars, holdout_bars, step_bars) <= 0:
        raise ValueError("window sizes must be positive")
    if validation_bars < max(cfg.min_candles, cfg.moving_average_period + 2) or holdout_bars < max(cfg.min_candles, cfg.moving_average_period + 2):
        raise ValueError("validation and holdout windows are too small")
    if len(rows) < train_bars + validation_bars + holdout_bars:
        raise ValueError("not enough candles for train, validation, and untouched holdout")
    invalid = _validate(symbol, rows, cfg, research_now)
    if invalid:
        raise ValueError(invalid)
    holdout_start = len(rows) - holdout_bars
    folds: list[SpotEvaluationWindow] = []
    start = train_bars
    fold_number = 1
    while start + validation_bars <= holdout_start:
        end = start + validation_bars
        warmup_start = max(0, start - max(train_bars, cfg.moving_average_period))
        context = rows[warmup_start:end]
        local_start = start - warmup_start
        local_end = end - warmup_start
        strategy = _simulate_window(symbol, context, local_start, local_end, cfg)
        folds.append(SpotEvaluationWindow(
            f"validation_{fold_number}", start, end, strategy,
            _buy_hold_return(rows[start:end], cfg), 0.0, False))
        start += step_bars
        fold_number += 1
    if not folds:
        raise ValueError("not enough pre-holdout history for a validation fold")
    warmup_start = max(0, holdout_start - max(train_bars, cfg.moving_average_period))
    holdout_context = rows[warmup_start:]
    local_start = holdout_start - warmup_start
    holdout_strategy = _simulate_window(symbol, holdout_context, local_start,
                                        len(holdout_context), cfg)
    holdout = SpotEvaluationWindow(
        "untouched_holdout", holdout_start, len(rows), holdout_strategy,
        _buy_hold_return(rows[holdout_start:], cfg), 0.0, False)
    return SpotWalkForwardResult(symbol, tuple(folds), holdout, (
        "Fixed strategy settings only; no hyperparameter tuning is performed.",
        "Final holdout is reserved and must not be used for model or parameter selection.",
        "Cash/no-trade return is 0% before interest; buy-and-hold uses the same position fraction and cost assumptions.",
        "Simplified OHLCV simulation omits spread variation, market impact, partial fills, and exchange outages.",
        "Research only; orders_authorized is false.",
    ), False)
