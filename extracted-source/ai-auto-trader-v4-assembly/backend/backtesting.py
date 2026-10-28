"""Small, deterministic historical simulator; never connects to an exchange."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Iterable

from backend.strategy_research import Candle, ResearchConfig, research_strategies


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    initial_equity: float = 10_000.0
    position_fraction: float = 0.10
    fee_bps_per_side: float = 4.0
    slippage_bps_per_side: float = 2.0
    funding_bps_per_8h: float = 1.0
    candle_interval_minutes: int = 1
    minimum_signal_score: float = 60.0

    def __post_init__(self) -> None:
        numeric = (self.initial_equity, self.position_fraction, self.fee_bps_per_side,
                   self.slippage_bps_per_side, self.funding_bps_per_8h,
                   self.minimum_signal_score)
        if not all(math.isfinite(v) for v in numeric):
            raise ValueError("backtest configuration values must be finite")
        if self.initial_equity <= 0:
            raise ValueError("initial_equity must be positive")
        if not 0 < self.position_fraction <= 1:
            raise ValueError("position_fraction must be in (0, 1]")
        if min(self.fee_bps_per_side, self.slippage_bps_per_side, self.funding_bps_per_8h) < 0:
            raise ValueError("cost assumptions must be non-negative")
        if self.candle_interval_minutes <= 0:
            raise ValueError("candle_interval_minutes must be positive")
        if not 0 <= self.minimum_signal_score <= 100:
            raise ValueError("minimum_signal_score must be between 0 and 100")


@dataclass(frozen=True, slots=True)
class SimulatedTrade:
    symbol: str
    direction: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    notional: float
    gross_pnl: float
    costs: float
    net_pnl: float
    strategy_consensus: str


@dataclass(frozen=True, slots=True)
class BacktestResult:
    symbol: str
    initial_equity: float
    ending_equity: float
    net_pnl: float
    total_costs: float
    trade_count: int
    wins: int
    losses: int
    win_rate_pct: float
    max_drawdown_pct: float
    trades: tuple[SimulatedTrade, ...]
    warnings: tuple[str, ...]
    orders_authorized: bool = False


def _drawdown_pct(equity_points: list[float]) -> float:
    peak = equity_points[0]
    max_drawdown = 0.0
    for equity in equity_points:
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak * 100.0)
    return round(max_drawdown, 4)


def run_backtest(
    symbol: str,
    candles: Iterable[Candle],
    *,
    config: BacktestConfig | None = None,
    research_config: ResearchConfig | None = None,
    research_now: datetime,
    start_timestamp: datetime | None = None,
) -> BacktestResult:
    """Simulate one-bar holds with next-open fills and explicit estimated costs.

    Signals use candles through index i; entry occurs at candle i+1 open and exit at
    candle i+2 open. This avoids filling at the same close that generated the signal.
    The simulator is intentionally simple and does not model stops, liquidation,
    order-book depth, or the sign of real funding payments.
    """
    if research_now.tzinfo is None or research_now.utcoffset() is None:
        raise ValueError("research_now must be timezone-aware")
    if start_timestamp is not None and (start_timestamp.tzinfo is None or start_timestamp.utcoffset() is None):
        raise ValueError("start_timestamp must be timezone-aware")
    cfg = config or BacktestConfig()
    strategy_cfg = research_config or ResearchConfig()
    rows = list(candles)
    initial = cfg.initial_equity
    if len(rows) < strategy_cfg.min_candles:
        return BacktestResult(symbol, initial, initial, 0.0, 0.0, 0, 0, 0, 0.0, 0.0,
                              (), (f"insufficient history: need at least {strategy_cfg.min_candles} candles",), False)

    # Validate the complete series and its latest timestamp before simulation.
    validation = research_strategies(symbol, rows,
                                     now=research_now, config=strategy_cfg)
    if validation.reasons and validation.decision == "NO_TRADE" and any(
        marker in validation.reasons[0].lower() for marker in
        ("missing", "mixed", "timestamp", "stale", "non-finite", "positive", "inconsistent")
    ):
        return BacktestResult(symbol, initial, initial, 0.0, 0.0, 0, 0, 0, 0.0, 0.0,
                              (), (f"input rejected: {validation.reasons[0]}",), False)

    # The configured interval is part of the dataset contract, not a hint.
    # Strict cadence validation prevents a missing or irregular bar from being
    # treated as one ordinary backtest step. Convert offsets to UTC before
    # comparing so timezone differences do not disguise interval gaps.
    expected_interval = timedelta(minutes=cfg.candle_interval_minutes)
    utc_stamps = [row.timestamp.astimezone(timezone.utc) for row in rows]
    for index, (previous_stamp, current_stamp) in enumerate(zip(utc_stamps, utc_stamps[1:]), start=1):
        elapsed = current_stamp - previous_stamp
        if elapsed != expected_interval:
            if elapsed > expected_interval:
                detail = (
                    f"candle interval gap before row {index + 1}: expected "
                    f"{cfg.candle_interval_minutes}-minute cadence, got {elapsed.total_seconds():g} seconds"
                )
            else:
                detail = (
                    f"candle interval mismatch before row {index + 1}: expected "
                    f"{cfg.candle_interval_minutes}-minute cadence, got {elapsed.total_seconds():g} seconds"
                )
            return BacktestResult(
                symbol, initial, initial, 0.0, 0.0, 0, 0, 0, 0.0, 0.0,
                (), (f"input rejected: {detail}",), False,
            )

    equity = initial
    total_costs = 0.0
    trades: list[SimulatedTrade] = []
    equity_points = [initial]
    i = strategy_cfg.min_candles - 1
    if start_timestamp is not None:
        start_utc = start_timestamp.astimezone(timezone.utc)
        # Signals may use prior candles as warmup, but no simulated entry may precede
        # the requested out-of-sample boundary.
        first_entry_index = next(
            (index for index, row in enumerate(rows) if row.timestamp.astimezone(timezone.utc) >= start_utc),
            len(rows),
        )
        i = max(i, first_entry_index - 1)
    while i < len(rows) - 2 and equity > 0:
        window = rows[max(0, i - strategy_cfg.min_candles + 1):i + 1]
        signal = research_strategies(symbol, window, now=window[-1].timestamp, config=strategy_cfg)
        direction = signal.decision.removeprefix("REVIEW_")
        if direction not in ("LONG", "SHORT"):
            i += 1
            continue
        directional_scores = [a.confidence_score for a in signal.assessments
                              if a.direction == direction and a.confidence_score >= cfg.minimum_signal_score]
        if len(directional_scores) < 2:
            i += 1
            continue

        entry = rows[i + 1]
        exit_candle = rows[i + 2]
        entry_price, exit_price = entry.open, exit_candle.open
        if not (math.isfinite(entry_price) and math.isfinite(exit_price)
                and entry_price > 0 and exit_price > 0):
            return BacktestResult(symbol, initial, equity, equity - initial, total_costs,
                                  len(trades), sum(t.net_pnl > 0 for t in trades),
                                  sum(t.net_pnl < 0 for t in trades),
                                  round(sum(t.net_pnl > 0 for t in trades) / len(trades) * 100, 2) if trades else 0.0,
                                  _drawdown_pct(equity_points), tuple(trades),
                                  ("invalid simulated fill price; stopped fail-closed",), False)
        notional = equity * cfg.position_fraction
        sign = 1.0 if direction == "LONG" else -1.0
        gross = notional * sign * (exit_price / entry_price - 1.0)
        turnover_cost_rate = 2.0 * (cfg.fee_bps_per_side + cfg.slippage_bps_per_side) / 10_000.0
        elapsed_minutes = max(0.0, (exit_candle.timestamp - entry.timestamp).total_seconds() / 60.0)
        funding_rate = cfg.funding_bps_per_8h / 10_000.0 * elapsed_minutes / 480.0
        costs = notional * turnover_cost_rate + notional * funding_rate
        net = gross - costs
        trade = SimulatedTrade(symbol, direction, entry.timestamp, exit_candle.timestamp,
                               entry_price, exit_price, round(notional, 8), round(gross, 8),
                               round(costs, 8), round(net, 8), signal.decision)
        trades.append(trade)
        equity = max(0.0, equity + net)
        total_costs += costs
        equity_points.append(equity)
        i += 2  # one simulated position at a time; no overlapping trades

    wins = sum(trade.net_pnl > 0 for trade in trades)
    losses = sum(trade.net_pnl < 0 for trade in trades)
    net_pnl = equity - initial
    return BacktestResult(
        symbol=symbol,
        initial_equity=initial,
        ending_equity=round(equity, 8),
        net_pnl=round(net_pnl, 8),
        total_costs=round(total_costs, 8),
        trade_count=len(trades),
        wins=wins,
        losses=losses,
        win_rate_pct=round(wins / len(trades) * 100.0, 2) if trades else 0.0,
        max_drawdown_pct=_drawdown_pct(equity_points),
        trades=tuple(trades),
        warnings=("Research simulation only; estimated costs and one-bar holds are simplified.",),
        orders_authorized=False,
    )
