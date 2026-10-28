"""Shared typed contracts used across scanner, strategy, validation, and risk layers."""
from dataclasses import dataclass
from typing import Literal

Side = Literal["LONG", "SHORT"]


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    mark_price: float
    observed_at: float
    is_fresh: bool


@dataclass(frozen=True)
class TradeProposal:
    proposal_id: str
    symbol: str
    side: Side
    quantity: float
    entry_price: float
    stop_loss: float
    take_profit: float
    estimated_risk: float
    signal_timestamp: float


@dataclass(frozen=True)
class RiskLimits:
    max_position_notional: float
    max_daily_loss: float
    max_open_positions: int
    max_risk_per_trade: float
    max_signal_age_seconds: float = 60.0
    max_drawdown_pct: float = 20.0
    max_mark_age_seconds: float = 30.0


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    code: str
    reason: str
    proposal_id: str
