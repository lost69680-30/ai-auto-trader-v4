"""Fail-closed validation for strategy proposals; never authorizes orders.

This module validates signal integrity and rule compliance. A VALID result is a
research hand-off to the independent risk guardian, not a trade instruction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Iterable


@dataclass(frozen=True, slots=True)
class StrategyVote:
    strategy: str
    direction: str
    strength: float


@dataclass(frozen=True, slots=True)
class SignalProposal:
    signal_id: str
    symbol: str
    direction: str
    created_at: datetime
    expires_at: datetime
    market_data_at: datetime
    entry_price: float
    stop_price: float
    target_price: float
    estimated_round_trip_cost_pct: float
    votes: tuple[StrategyVote, ...]


@dataclass(frozen=True, slots=True)
class SignalValidationConfig:
    max_signal_age_seconds: int = 120
    max_market_data_age_seconds: int = 30
    max_future_skew_seconds: int = 2
    minimum_vote_strength: float = 60.0
    minimum_agreeing_strategies: int = 2
    minimum_net_reward_risk: float = 1.25
    maximum_round_trip_cost_pct: float = 1.0

    def __post_init__(self) -> None:
        if self.max_signal_age_seconds <= 0 or self.max_market_data_age_seconds <= 0:
            raise ValueError("signal and market-data age limits must be positive")
        if self.max_future_skew_seconds < 0:
            raise ValueError("future timestamp tolerance must be non-negative")
        if not math.isfinite(self.minimum_vote_strength) or not 0 <= self.minimum_vote_strength <= 100:
            raise ValueError("minimum_vote_strength must be between 0 and 100")
        if self.minimum_agreeing_strategies < 1:
            raise ValueError("minimum_agreeing_strategies must be at least 1")
        if not math.isfinite(self.minimum_net_reward_risk) or self.minimum_net_reward_risk <= 0:
            raise ValueError("minimum_net_reward_risk must be finite and positive")
        if not math.isfinite(self.maximum_round_trip_cost_pct) or self.maximum_round_trip_cost_pct <= 0:
            raise ValueError("maximum_round_trip_cost_pct must be finite and positive")


@dataclass(frozen=True, slots=True)
class SignalValidationResult:
    signal_id: str
    symbol: str
    direction: str
    status: str
    reasons: tuple[str, ...]
    agreeing_strategies: tuple[str, ...]
    opposing_strategies: tuple[str, ...]
    gross_reward_risk: float
    net_reward_risk: float
    orders_authorized: bool = False


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("all timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _result(signal: SignalProposal, status: str, reasons: Iterable[str],
            agreeing: Iterable[str] = (), opposing: Iterable[str] = (),
            gross_rr: float = 0.0, net_rr: float = 0.0) -> SignalValidationResult:
    return SignalValidationResult(
        signal_id=signal.signal_id,
        symbol=signal.symbol,
        direction=signal.direction,
        status=status,
        reasons=tuple(reasons),
        agreeing_strategies=tuple(sorted(set(agreeing))),
        opposing_strategies=tuple(sorted(set(opposing))),
        gross_reward_risk=round(gross_rr, 4),
        net_reward_risk=round(net_rr, 4),
        orders_authorized=False,
    )


def validate_signal(
    signal: SignalProposal,
    *,
    now: datetime,
    config: SignalValidationConfig | None = None,
    previously_seen_ids: set[str] | frozenset[str] = frozenset(),
) -> SignalValidationResult:
    """Validate freshness, price geometry, strategy consensus, and net reward/risk.

    Outcomes are VALID, WAIT_RECHECK, or REJECT. VALID means eligible for the
    independent risk guardian only. This function has no exchange or order API.
    """
    now_utc = _aware_utc(now)
    created = _aware_utc(signal.created_at)
    expires = _aware_utc(signal.expires_at)
    market_data = _aware_utc(signal.market_data_at)
    config = config or SignalValidationConfig()

    errors: list[str] = []
    if not signal.signal_id.strip() or not signal.symbol.strip():
        errors.append("signal id and symbol are required")
    if signal.signal_id in previously_seen_ids:
        errors.append("duplicate signal id has already been processed")
    if signal.direction not in ("LONG", "SHORT"):
        errors.append("direction must be LONG or SHORT")
    if created > now_utc + timedelta(seconds=config.max_future_skew_seconds):
        errors.append("signal creation timestamp is in the future")
    if market_data > now_utc + timedelta(seconds=config.max_future_skew_seconds):
        errors.append("market-data timestamp is in the future")
    if expires <= now_utc:
        errors.append("signal has expired")
    if expires <= created:
        errors.append("signal expiry must be later than creation time")
    if (now_utc - created).total_seconds() > config.max_signal_age_seconds:
        errors.append("signal is stale")
    if (now_utc - market_data).total_seconds() > config.max_market_data_age_seconds:
        errors.append("market data is stale")

    numeric = (signal.entry_price, signal.stop_price, signal.target_price,
               signal.estimated_round_trip_cost_pct)
    if not all(math.isfinite(value) for value in numeric):
        errors.append("prices and estimated costs must be finite numbers")
    elif min(signal.entry_price, signal.stop_price, signal.target_price) <= 0:
        errors.append("entry, stop, and target prices must be positive")
    elif signal.estimated_round_trip_cost_pct < 0:
        errors.append("estimated round-trip costs must be non-negative")
    elif signal.estimated_round_trip_cost_pct > config.maximum_round_trip_cost_pct:
        errors.append("estimated transaction costs exceed configured maximum")

    votes = tuple(signal.votes)
    strategy_names = [vote.strategy.strip() for vote in votes]
    if any(not name for name in strategy_names):
        errors.append("strategy vote names must be non-empty")
    if len(strategy_names) != len(set(strategy_names)):
        errors.append("duplicate strategy votes are not allowed")
    for vote in votes:
        if vote.direction not in ("LONG", "SHORT", "NEUTRAL"):
            errors.append(f"invalid direction in strategy vote: {vote.strategy}")
        if not math.isfinite(vote.strength) or not 0 <= vote.strength <= 100:
            errors.append(f"invalid strength in strategy vote: {vote.strategy}")

    if errors:
        return _result(signal, "REJECT", errors)

    if signal.direction == "LONG":
        geometry_valid = signal.stop_price < signal.entry_price < signal.target_price
        risk_pct = (signal.entry_price - signal.stop_price) / signal.entry_price * 100.0
        reward_pct = (signal.target_price - signal.entry_price) / signal.entry_price * 100.0
    else:
        geometry_valid = signal.target_price < signal.entry_price < signal.stop_price
        risk_pct = (signal.stop_price - signal.entry_price) / signal.entry_price * 100.0
        reward_pct = (signal.entry_price - signal.target_price) / signal.entry_price * 100.0
    if not geometry_valid:
        return _result(signal, "REJECT", ("stop/target geometry is invalid for signal direction",))
    if risk_pct <= 0 or reward_pct <= 0:
        return _result(signal, "REJECT", ("stop risk and target reward must be positive",))

    cost_pct = signal.estimated_round_trip_cost_pct
    net_reward_pct = reward_pct - cost_pct
    net_risk_pct = risk_pct + cost_pct
    gross_rr = reward_pct / risk_pct
    net_rr = net_reward_pct / net_risk_pct if net_reward_pct > 0 and net_risk_pct > 0 else 0.0
    if net_reward_pct <= 0 or net_rr < config.minimum_net_reward_risk:
        return _result(signal, "REJECT", (
            f"net reward/risk {net_rr:.3f} is below required {config.minimum_net_reward_risk:.3f}",
        ), gross_rr=gross_rr, net_rr=net_rr)

    strong = [vote for vote in votes if vote.strength >= config.minimum_vote_strength
              and vote.direction in ("LONG", "SHORT")]
    agreeing = [vote.strategy for vote in strong if vote.direction == signal.direction]
    opposing = [vote.strategy for vote in strong if vote.direction != signal.direction]
    if opposing:
        return _result(signal, "WAIT_RECHECK", (
            "strong strategy votes conflict with the proposed direction",
        ), agreeing, opposing, gross_rr, net_rr)
    if len(agreeing) < config.minimum_agreeing_strategies:
        return _result(signal, "WAIT_RECHECK", (
            f"need {config.minimum_agreeing_strategies} strong agreeing strategies; found {len(agreeing)}",
        ), agreeing, opposing, gross_rr, net_rr)

    return _result(signal, "VALID", (
        "signal passed deterministic validation; independent risk approval is still required",
    ), agreeing, opposing, gross_rr, net_rr)
