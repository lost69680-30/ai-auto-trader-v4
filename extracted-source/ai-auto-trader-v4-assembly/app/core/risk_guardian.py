"""Deterministic fail-closed order gate. It never submits orders."""
from __future__ import annotations

import math

from .models import MarketSnapshot, RiskDecision, RiskLimits, TradeProposal


MAX_DAILY_LOSS_FRACTION = 0.02
MAX_SINGLE_ASSET_ALLOCATION_FRACTION = 0.10
MAX_GROSS_LEVERAGE = 1.0


def _reject(proposal: TradeProposal, code: str, reason: str) -> RiskDecision:
    proposal_id = proposal.proposal_id if isinstance(proposal.proposal_id, str) else ""
    return RiskDecision(False, code, reason, proposal_id)


def _finite_number(value: object) -> bool:
    """Accept finite JSON-like numbers, but not booleans (which subclass int)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _canonical_symbol(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip().upper()


def evaluate_proposal(
    proposal: TradeProposal,
    market: MarketSnapshot,
    limits: RiskLimits,
    *,
    daily_pnl: float,
    open_positions: int,
    now: float,
    equity_baseline: float | None = None,
    same_asset_exposure: float = 0.0,
    gross_exposure: float = 0.0,
) -> RiskDecision:
    """Return a pre-trade approval/veto decision, never an exchange order.

    The guardian validates numeric/config inputs before comparisons so NaN and
    infinity can never bypass ordinary ``>``/``<`` risk checks. Stop-distance
    risk is recomputed here and compared with the caller's estimate using the
    more conservative of the two values.
    """
    if not isinstance(proposal, TradeProposal):
        return RiskDecision(False, "INVALID_INPUT", "Trade proposal has an invalid type.", "")
    if not isinstance(market, MarketSnapshot) or not isinstance(limits, RiskLimits):
        return _reject(proposal, "INVALID_INPUT", "Market snapshot or risk-limit object has an invalid type.")

    proposal_numeric = (
        proposal.quantity, proposal.entry_price, proposal.stop_loss,
        proposal.take_profit, proposal.estimated_risk, proposal.signal_timestamp,
    )
    limit_numeric = (
        limits.max_position_notional, limits.max_daily_loss,
        limits.max_risk_per_trade, limits.max_signal_age_seconds,
        limits.max_drawdown_pct, limits.max_mark_age_seconds,
    )
    if not all(_finite_number(value) for value in proposal_numeric + limit_numeric):
        return _reject(proposal, "INVALID_INPUT", "Proposal values and risk limits must be finite numeric values.")
    if not _finite_number(market.mark_price) or not _finite_number(market.observed_at):
        return _reject(proposal, "INVALID_INPUT", "Market price and timestamp must be finite numeric values.")
    if not _finite_number(daily_pnl) or not _finite_number(now):
        return _reject(proposal, "INVALID_INPUT", "Daily P&L and evaluation timestamp must be finite numeric values.")
    if not _finite_number(equity_baseline) or float(equity_baseline) <= 0:
        return _reject(proposal, "INVALID_INPUT", "A positive formal equity baseline is required for risk validation.")
    if not _finite_number(same_asset_exposure) or not _finite_number(gross_exposure):
        return _reject(proposal, "INVALID_INPUT", "Existing asset and gross exposures must be finite numeric values.")
    if same_asset_exposure < 0 or gross_exposure < 0:
        return _reject(proposal, "INVALID_INPUT", "Existing asset and gross exposures cannot be negative.")
    if isinstance(open_positions, bool) or not isinstance(open_positions, int) or open_positions < 0:
        return _reject(proposal, "INVALID_INPUT", "Open-position count must be a non-negative integer.")
    if not isinstance(market.is_fresh, bool):
        return _reject(proposal, "INVALID_INPUT", "Market freshness flag must be boolean.")
    if not isinstance(proposal.proposal_id, str) or not proposal.proposal_id.strip():
        return _reject(proposal, "INVALID_INPUT", "Proposal identifier must be a non-empty string.")
    if not _canonical_symbol(proposal.symbol) or not _canonical_symbol(market.symbol):
        return _reject(proposal, "INVALID_INPUT", "Proposal and market symbols must be trimmed uppercase strings.")
    if (
        limits.max_position_notional <= 0
        or limits.max_daily_loss <= 0
        or limits.max_risk_per_trade <= 0
        or limits.max_signal_age_seconds <= 0
        or limits.max_drawdown_pct <= 0
        or limits.max_mark_age_seconds <= 0
        or isinstance(limits.max_open_positions, bool)
        or not isinstance(limits.max_open_positions, int)
        or limits.max_open_positions < 0
    ):
        return _reject(proposal, "INVALID_INPUT", "Risk limits must be positive, finite and internally valid.")
    if (
        proposal.quantity <= 0 or proposal.entry_price <= 0
        or proposal.stop_loss <= 0 or proposal.take_profit <= 0
        or proposal.estimated_risk < 0 or proposal.signal_timestamp < 0
        or market.mark_price <= 0 or market.observed_at < 0 or now < 0
    ):
        return _reject(proposal, "INVALID_INPUT", "Prices, quantity, timestamps, risk estimates, or limits are out of range.")

    if not market.is_fresh or now < market.observed_at:
        return _reject(proposal, "STALE_MARKET_DATA", "Market data is stale or timestamp is invalid.")
    if market.symbol != proposal.symbol:
        return _reject(proposal, "SYMBOL_MISMATCH", "Proposal symbol does not match validated market data.")
    if now - proposal.signal_timestamp > limits.max_signal_age_seconds or proposal.signal_timestamp > now:
        return _reject(proposal, "STALE_SIGNAL", "Signal is expired or has a future timestamp.")
    daily_loss_budget = min(limits.max_daily_loss, float(equity_baseline) * MAX_DAILY_LOSS_FRACTION)
    if daily_pnl <= -daily_loss_budget:
        return _reject(proposal, "DAILY_LOSS_LIMIT", "Daily loss limit (capped at 2% of formal equity) has been reached; no new risk is allowed.")
    if open_positions >= limits.max_open_positions:
        return _reject(proposal, "OPEN_POSITION_LIMIT", "Maximum open-position count has been reached.")

    if proposal.side == "LONG":
        valid_geometry = proposal.stop_loss < proposal.entry_price < proposal.take_profit
    elif proposal.side == "SHORT":
        valid_geometry = proposal.take_profit < proposal.entry_price < proposal.stop_loss
    else:
        valid_geometry = False
    if not valid_geometry:
        return _reject(proposal, "INVALID_STOP_GEOMETRY", "Stop-loss and take-profit must be on the correct side of entry.")

    notional = proposal.quantity * proposal.entry_price
    derived_risk = proposal.quantity * abs(proposal.entry_price - proposal.stop_loss)
    if not math.isfinite(notional) or not math.isfinite(derived_risk):
        return _reject(proposal, "INVALID_INPUT", "Derived notional or stop-distance risk is non-finite.")
    if notional > limits.max_position_notional:
        return _reject(proposal, "POSITION_NOTIONAL_LIMIT", "Proposed notional exceeds the configured position limit.")
    asset_allocation_cap = float(equity_baseline) * MAX_SINGLE_ASSET_ALLOCATION_FRACTION
    if same_asset_exposure + notional > asset_allocation_cap:
        return _reject(proposal, "ASSET_ALLOCATION_LIMIT", "Proposed order would exceed the 10% single-asset allocation cap.")
    gross_exposure_cap = float(equity_baseline) * MAX_GROSS_LEVERAGE
    if gross_exposure + notional > gross_exposure_cap:
        return _reject(proposal, "LEVERAGE_LIMIT", "Proposed order would exceed the 1x gross-exposure cap.")
    conservative_risk = max(proposal.estimated_risk, derived_risk)
    if conservative_risk > limits.max_risk_per_trade:
        return _reject(proposal, "TRADE_RISK_LIMIT", "Estimated or recomputed loss at stop exceeds the per-trade risk budget.")
    return RiskDecision(True, "APPROVED", "Proposal passed deterministic pre-trade checks.", proposal.proposal_id)
