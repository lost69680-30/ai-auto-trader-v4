"""Explicit, conservative position sizing for the paper-only orchestration path.

Sizing uses stop distance and a caller-supplied simulated equity/risk fraction,
then caps notional and floors quantity to the configured contract step. It has
no exchange execution capability and does not infer leverage or contract rules.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
import math

from app.core.models import MarketSnapshot, RiskLimits, TradeProposal
from app.engines.orchestrator import OrchestrationRequest
from backend.signal_watcher import MarketTick


@dataclass(frozen=True, slots=True)
class PaperSizingConfig:
    simulated_equity: float
    risk_fraction: float = 0.005
    quantity_step: float = 0.001
    min_quantity: float = 0.001

    def __post_init__(self) -> None:
        for name in ("simulated_equity", "risk_fraction", "quantity_step", "min_quantity"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if self.simulated_equity <= 0:
            raise ValueError("simulated_equity must be positive")
        if not 0 < self.risk_fraction <= 0.02:
            raise ValueError("risk_fraction must be greater than zero and no more than 2%")
        if self.quantity_step <= 0 or self.min_quantity <= 0:
            raise ValueError("quantity_step and min_quantity must be positive")


def build_paper_orchestration_request(
    *, candidate: object, handoff: object, tick: MarketTick, now: datetime,
    equity: float, risk_fraction: float, quantity_step: float, min_quantity: float,
    limits: RiskLimits, daily_pnl: float, open_positions: int,
    max_tick_age_seconds: float = 10.0,
) -> OrchestrationRequest:
    """Build a risk-sized request; rejects malformed inputs instead of guessing."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(timezone.utc)
    cfg = PaperSizingConfig(equity, risk_fraction, quantity_step, min_quantity)
    if getattr(handoff, "orders_authorized", True):
        raise ValueError("signal handoff authorization flag is forbidden")
    if getattr(handoff, "status", None) != "VALID":
        raise ValueError("signal handoff must be VALID")
    signal = getattr(handoff, "signal", None)
    validation = getattr(handoff, "validation", None)
    if signal is None or validation is None or getattr(validation, "status", None) != "VALID":
        raise ValueError("validated signal and validation result are required")
    symbol = getattr(candidate, "symbol", None)
    direction = getattr(candidate, "direction", None)
    if symbol != signal.symbol or direction != signal.direction or tick.symbol != symbol:
        raise ValueError("candidate, signal, and tick symbol/direction must match")
    if not isinstance(tick.observed_at, datetime) or tick.observed_at.tzinfo is None or tick.observed_at.utcoffset() is None:
        raise ValueError("tick timestamp must be timezone-aware")
    tick_time = tick.observed_at.astimezone(timezone.utc)
    if tick_time > now:
        raise ValueError("tick timestamp is in the future")
    if isinstance(max_tick_age_seconds, bool) or not isinstance(max_tick_age_seconds, (int, float)) or not math.isfinite(max_tick_age_seconds) or max_tick_age_seconds <= 0:
        raise ValueError("max_tick_age_seconds must be finite and positive")
    if (now - tick_time).total_seconds() > max_tick_age_seconds:
        raise ValueError("tick is stale for paper request sizing")
    if not math.isfinite(tick.price) or tick.price <= 0:
        raise ValueError("tick price must be finite and positive")
    if not math.isfinite(daily_pnl):
        raise ValueError("daily_pnl must be finite")
    if isinstance(open_positions, bool) or not isinstance(open_positions, int) or open_positions < 0:
        raise ValueError("open_positions must be a non-negative integer")

    entry = float(signal.entry_price)
    stop = float(signal.stop_price)
    target = float(signal.target_price)
    if not all(math.isfinite(x) and x > 0 for x in (entry, stop, target)):
        raise ValueError("signal prices must be finite and positive")
    if direction == "LONG":
        geometry_ok = stop < entry < target
    elif direction == "SHORT":
        geometry_ok = target < entry < stop
    else:
        geometry_ok = False
    if not geometry_ok:
        raise ValueError("signal stop/entry/target geometry is invalid")

    risk_budget = min(cfg.simulated_equity * cfg.risk_fraction, limits.max_risk_per_trade)
    stop_distance = abs(entry - stop)
    raw_qty = min(risk_budget / stop_distance, limits.max_position_notional / entry)
    step = Decimal(str(cfg.quantity_step))
    quantity = float((Decimal(str(raw_qty)) / step).to_integral_value(rounding=ROUND_FLOOR) * step)
    if quantity < cfg.min_quantity:
        raise ValueError("minimum quantity exceeds configured risk or notional budget")
    proposal = TradeProposal(
        proposal_id=f"paper:{signal.signal_id}", symbol=symbol, side=direction,
        quantity=quantity, entry_price=entry, stop_loss=stop, take_profit=target,
        estimated_risk=quantity * stop_distance, signal_timestamp=signal.created_at.timestamp(),
    )
    return OrchestrationRequest(
        proposal=proposal,
        market=MarketSnapshot(symbol=symbol, mark_price=float(tick.price),
                              observed_at=tick_time.timestamp(), is_fresh=True),
        validation=validation, watch_result=None, limits=limits, daily_pnl=float(daily_pnl),
        open_positions=open_positions, now=now.timestamp(), initial_equity=cfg.simulated_equity,
    )
