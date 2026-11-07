"""Chronological, offline walk-forward evaluation with an untouched final holdout.

This evaluates a fixed research strategy; it does not tune or select parameters.
All outputs are research-only and can never authorize orders.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Iterable

from backend.backtesting import BacktestConfig, BacktestResult, run_backtest
from backend.external_strategy_intelligence import ExternalRuleSpec
from backend.market_data_lineage import MarketDataManifest
from backend.dataset_lineage import DatasetProvenance, assert_independent_dataset, candle_dataset_sha256
from backend.strategy_research import Candle, ResearchConfig


@dataclass(frozen=True, slots=True)
class WalkForwardConfig:
    train_bars: int = 2_000
    validation_bars: int = 500
    holdout_bars: int = 500
    step_bars: int = 500
    candle_interval_minutes: int = 1

    def __post_init__(self) -> None:
        geometry = (
            self.train_bars,
            self.validation_bars,
            self.holdout_bars,
            self.step_bars,
            self.candle_interval_minutes,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in geometry):
            raise ValueError("walk-forward fold geometry and candle interval must be integers")
        if self.train_bars < 30:
            raise ValueError("train_bars must be at least 30")
        if self.validation_bars < 32:
            raise ValueError("validation_bars must be at least 32 to allow warmup and trades")
        if self.holdout_bars < 32:
            raise ValueError("holdout must be at least 32 bars to allow warmup and trades")
        if self.step_bars < self.validation_bars:
            raise ValueError("step_bars must be at least validation_bars to keep OOS windows disjoint")
        if self.candle_interval_minutes <= 0:
            raise ValueError("candle interval must be positive")


@dataclass(frozen=True, slots=True)
class MarketRegimeEvidence:
    trend_state: str
    volatility_state: str
    trend_return_pct: float
    realized_volatility_pct: float
    observations: int
    methodology: str
    orders_authorized: bool = False


def _classify_market_regime(rows: list[Candle]) -> MarketRegimeEvidence:
    if len(rows) < 3:
        raise ValueError("market regime classification requires at least three candles")
    returns = [(rows[i].close / rows[i - 1].close - 1.0) * 100.0 for i in range(1, len(rows))]
    if not all(math.isfinite(value) for value in returns):
        raise ValueError("market regime returns must be finite")
    mean_return = math.fsum(returns) / len(returns)
    variance = math.fsum((value - mean_return) ** 2 for value in returns) / len(returns)
    volatility = math.sqrt(variance)
    trend = (rows[-1].close / rows[0].close - 1.0) * 100.0
    if not math.isfinite(volatility) or not math.isfinite(trend):
        raise ValueError("market regime metrics must be finite")
    return MarketRegimeEvidence(
        trend_state="UP" if trend >= 1.0 else "DOWN" if trend <= -1.0 else "RANGE",
        volatility_state="LOW" if volatility < 0.25 else "MEDIUM" if volatility < 1.0 else "HIGH",
        trend_return_pct=round(trend, 6), realized_volatility_pct=round(volatility, 6),
        observations=len(returns),
        methodology="Close-to-close trend thresholds ±1%; population standard deviation of percentage returns: LOW <0.25%, MEDIUM <1.0%, otherwise HIGH.",
        orders_authorized=False,
    )


@dataclass(frozen=True, slots=True)
class EvaluationWindow:
    name: str
    start_index: int
    end_index: int  # exclusive
    strategy: BacktestResult
    market_regime: MarketRegimeEvidence
    buy_hold_return_pct: float
    cash_return_pct: float = 0.0
    used_for_selection: bool = False


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    symbol: str
    folds: tuple[EvaluationWindow, ...]
    holdout: EvaluationWindow
    warnings: tuple[str, ...]
    orders_authorized: bool = False
    dataset_provenance: DatasetProvenance | None = None
    market_data_manifest: MarketDataManifest | None = None


def _canonical_timeframe(minutes: int) -> str:
    known = {1: "1m", 3: "3m", 5: "5m", 15: "15m", 30: "30m", 60: "1h", 120: "2h", 240: "4h", 360: "6h", 480: "8h", 720: "12h", 1440: "1d", 4320: "3d", 10080: "1w"}
    return known.get(minutes, f"{minutes}m")


def _validate_dataset(symbol: str, rows: list[Candle], interval_minutes: int) -> None:
    if not symbol or not rows:
        raise ValueError("symbol and candle data are required")
    previous = None
    expected = timedelta(minutes=interval_minutes)
    for index, row in enumerate(rows):
        if row.symbol != symbol:
            raise ValueError(f"mixed symbol at row {index}")
        if row.timestamp.tzinfo is None or row.timestamp.utcoffset() is None:
            raise ValueError(f"timestamp at row {index} must be timezone-aware")
        stamp = row.timestamp.astimezone(timezone.utc)
        if previous is not None and stamp - previous != expected:
            raise ValueError(f"candle interval mismatch at row {index}; expected contiguous cadence")
        values = (row.open, row.high, row.low, row.close, row.volume)
        if not all(math.isfinite(v) for v in values):
            raise ValueError(f"non-finite candle value at row {index}")
        if min(row.open, row.high, row.low, row.close) <= 0 or row.volume < 0:
            raise ValueError(f"invalid candle values at row {index}")
        if row.high < max(row.open, row.close, row.low) or row.low > min(row.open, row.close, row.high):
            raise ValueError(f"inconsistent OHLC values at row {index}")
        previous = stamp


def _evaluate_window(
    symbol: str,
    rows: list[Candle],
    start: int,
    end: int,
    name: str,
    backtest_config: BacktestConfig,
    research_config: ResearchConfig,
    train_bars: int,
    rule_spec: ExternalRuleSpec | None,
) -> EvaluationWindow:
    # Prior training-window candles are indicator warmup only. The backtest explicitly
    # blocks entries before the OOS boundary; benchmark returns use only the test segment.
    context_start = max(0, start - train_bars)
    context_rows = rows[context_start:end]
    test_rows = rows[start:end]
    now = context_rows[-1].timestamp.astimezone(timezone.utc)
    if rule_spec is not None:
        raise ValueError(
            "external rule specs are not supported by the current backtest engine"
        )
    strategy = run_backtest(
        symbol, context_rows, config=backtest_config, research_config=research_config,
        research_now=now, start_timestamp=test_rows[0].timestamp,
    )
    entry_price = test_rows[0].open
    exit_price = test_rows[-1].close
    gross_return = exit_price / entry_price - 1.0
    round_trip_cost = 2.0 * (
        backtest_config.fee_bps_per_side + backtest_config.slippage_bps_per_side
    ) / 10_000.0
    # Buy-and-hold is a long-only benchmark; this deliberately does not model dividends,
    # borrow, funding, spread variation, or market impact.
    buy_hold_return = (gross_return - round_trip_cost) * backtest_config.position_fraction * 100.0
    return EvaluationWindow(
        name=name,
        start_index=start,
        end_index=end,
        strategy=strategy,
        market_regime=_classify_market_regime(test_rows),
        buy_hold_return_pct=round(buy_hold_return, 6),
        cash_return_pct=0.0,
        used_for_selection=False,
    )


def evaluate_walk_forward(
    symbol: str,
    candles: Iterable[Candle],
    *,
    config: WalkForwardConfig | None = None,
    backtest_config: BacktestConfig | None = None,
    research_config: ResearchConfig | None = None,
    research_now: datetime,
    dataset_provider: str | None = None,
    dataset_venue: str | None = None,
    reference_datasets: Iterable[DatasetProvenance] = (),
    rule_spec: ExternalRuleSpec | None = None,
) -> WalkForwardResult:
    """Evaluate fixed strategy settings over rolling chronological windows.

    The final holdout is reserved before folds are constructed and is never used for
    configuration or selection. This harness intentionally does not perform hyperparameter
    tuning: any tuning must be done only within each fold's training window by a separate,
    pre-registered procedure. Here, train_bars defines the initial historical warmup window.
    """
    if research_now.tzinfo is None or research_now.utcoffset() is None:
        raise ValueError("research_now must be timezone-aware")
    cfg = config or WalkForwardConfig()
    bt_cfg = backtest_config or BacktestConfig(candle_interval_minutes=cfg.candle_interval_minutes)
    rs_cfg = research_config or ResearchConfig()
    rows = list(candles)
    _validate_dataset(symbol, rows, cfg.candle_interval_minutes)
    provenance = None
    if dataset_provider is not None or dataset_venue is not None:
        if not dataset_provider or not dataset_venue:
            raise ValueError("dataset_provider and dataset_venue must be supplied together")
        provenance = DatasetProvenance(
            provider=dataset_provider, venue=dataset_venue, symbol=symbol,
            timeframe=_canonical_timeframe(cfg.candle_interval_minutes),
            window_start=rows[0].timestamp, window_end=rows[-1].timestamp,
            candle_count=len(rows), data_sha256=candle_dataset_sha256(rows),
        )
        for reference in reference_datasets:
            assert_independent_dataset(provenance, reference)
    elif tuple(reference_datasets):
        raise ValueError("dataset provenance is required when reference datasets are supplied")
    if rows and rows[-1].timestamp.astimezone(timezone.utc) > (
        research_now.astimezone(timezone.utc)
        + timedelta(seconds=rs_cfg.max_future_skew_seconds)
    ):
        raise ValueError("candle dataset contains future timestamps relative to research_now")
    minimum = cfg.train_bars + cfg.validation_bars + cfg.holdout_bars
    if len(rows) < minimum:
        raise ValueError(f"not enough candles: need at least {minimum}, got {len(rows)}")
    if bt_cfg.candle_interval_minutes != cfg.candle_interval_minutes:
        raise ValueError("backtest and walk-forward candle intervals must match")
    if cfg.validation_bars < rs_cfg.min_candles + 2 or cfg.holdout_bars < rs_cfg.min_candles + 2:
        raise ValueError("validation and holdout windows must allow strategy warmup and trades")

    holdout_start = len(rows) - cfg.holdout_bars
    folds: list[EvaluationWindow] = []
    test_start = cfg.train_bars
    fold_number = 1
    while test_start + cfg.validation_bars <= holdout_start:
        folds.append(_evaluate_window(
            symbol, rows, test_start, test_start + cfg.validation_bars,
            f"walk_forward_{fold_number}", bt_cfg, rs_cfg, cfg.train_bars, rule_spec,
        ))
        fold_number += 1
        test_start += cfg.step_bars
    if not folds:
        raise ValueError("not enough pre-holdout history for at least one validation fold")
    holdout = _evaluate_window(
        symbol, rows, holdout_start, len(rows), "untouched_holdout", bt_cfg, rs_cfg, cfg.train_bars, rule_spec
    )
    return WalkForwardResult(
        symbol=symbol,
        folds=tuple(folds),
        holdout=holdout,
        warnings=(
            "Fixed strategy settings only; no parameter optimization is performed by this harness.",
            "The final holdout is reported once and must not be used to tune strategy settings.",
            "Buy-and-hold is a simplified long-only benchmark; all returns are historical estimates, not forecasts.",
            "The simulator uses simplified fills, one-bar holds, and estimated costs; it does not model stops or market impact.",
            "Research only: orders_authorized is false.",
        ),
        orders_authorized=False,
        dataset_provenance=provenance,
    )
