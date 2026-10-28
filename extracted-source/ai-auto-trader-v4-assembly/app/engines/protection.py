"""Durable emergency stop and automatic paper-position protection.

This module only closes local simulated positions. It has no exchange adapter,
network access, or authority to submit live orders. Price freshness is a duty of
the market-data adapter; this module validates timestamps for ordering but cannot
prove that an externally supplied quote is fresh.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping

from app.engines.paper_ledger import PaperPosition, SQLitePaperLedger


@dataclass(frozen=True, slots=True)
class ProtectionResult:
    status: str
    reason: str
    marked_positions: tuple[PaperPosition, ...] = ()
    closed_positions: tuple[PaperPosition, ...] = ()
    close_reasons: Mapping[str, str] = field(default_factory=dict)
    cancelled_order_ids: tuple[str, ...] = ()
    emergency_stop_active: bool = False
    orders_authorized: bool = False


class PaperProtectionEngine:
    """Evaluate persisted marks, SL/TP levels, optional trailing stops, and a hard stop.

    ``trailing_distance_pct`` and ``trailing_activation_pct`` are decimal fractions
    (``0.02`` means 2%). A hard stop always blocks new intents/fills through the
    ledger. If no prices are supplied on activation, open positions are not closed
    at fabricated/stale prices; a later price update is required to paper-flatten.
    """

    def __init__(
        self,
        ledger: SQLitePaperLedger,
        *,
        trailing_distance_pct: float | None = None,
        trailing_activation_pct: float = 0.0,
        fee_rate: float = 0.0004,
        slippage_bps: float = 2.0,
    ) -> None:
        self._ledger = ledger
        self._validate_finite("fee_rate", fee_rate)
        self._validate_finite("slippage_bps", slippage_bps)
        self._validate_finite("trailing_activation_pct", trailing_activation_pct)
        if not 0 <= fee_rate <= 1:
            raise ValueError("fee_rate must be between 0 and 1 inclusive.")
        if not 0 <= slippage_bps < 10_000:
            raise ValueError("slippage_bps must be at least 0 and below 10000.")
        if not 0 <= trailing_activation_pct < 1:
            raise ValueError("trailing_activation_pct must be between 0 and 1.")
        if trailing_distance_pct is not None:
            self._validate_finite("trailing_distance_pct", trailing_distance_pct)
            if not 0 < trailing_distance_pct < 1:
                raise ValueError("trailing_distance_pct must be greater than 0 and below 1.")
        self._trailing_distance_pct = trailing_distance_pct
        self._trailing_activation_pct = trailing_activation_pct
        self._fee_rate = fee_rate
        self._slippage_bps = slippage_bps

    @property
    def emergency_stop_active(self) -> bool:
        return self._ledger.emergency_stop_active

    def activate_emergency_stop(
        self,
        *,
        observed_at: float,
        reason: str,
        market_prices: Mapping[str, float] | None = None,
    ) -> ProtectionResult:
        """Persist stop first, cancel pending intents, then optionally flatten on supplied prices."""
        cancelled = self._ledger.activate_emergency_stop(occurred_at=observed_at, reason=reason)
        if market_prices is None:
            return ProtectionResult(
                status="EMERGENCY_STOP_ACTIVE",
                reason=(
                    "Emergency stop persisted and pending intents cancelled. Open positions remain open "
                    "until valid prices are supplied; no close price was fabricated."
                ),
                cancelled_order_ids=cancelled,
                emergency_stop_active=True,
            )
        result = self.process_market_update(market_prices, observed_at=observed_at)
        return ProtectionResult(
            status=result.status,
            reason=result.reason,
            marked_positions=result.marked_positions,
            closed_positions=result.closed_positions,
            close_reasons=result.close_reasons,
            cancelled_order_ids=cancelled,
            emergency_stop_active=True,
        )

    def reset_emergency_stop(self, *, observed_at: float, reason: str) -> bool:
        """Require a manual reason and flat paper positions before re-arming entry flow."""
        if self._ledger.open_positions:
            raise ValueError("Emergency stop cannot be reset while paper positions remain open.")
        return self._ledger.clear_emergency_stop(occurred_at=observed_at, reason=reason)

    def process_market_update(
        self,
        market_prices: Mapping[str, float],
        *,
        observed_at: float,
    ) -> ProtectionResult:
        """Persist a market update, execute paper protection, and recover pending exits.

        Existing persisted marks are checked before overwriting them. That closes the
        crash window where a previous process stored a threshold-crossing mark but
        exited before it could record the simulated close.
        """
        self._validate_finite("observed_at", observed_at)
        if observed_at < 0:
            raise ValueError("observed_at must be non-negative.")
        normalized_prices: dict[str, float] = {}
        for symbol, price in market_prices.items():
            if not isinstance(symbol, str) or not symbol.strip():
                raise ValueError("Market-price symbols must be non-empty strings.")
            key = symbol.strip().upper()
            if key in normalized_prices and normalized_prices[key] != price:
                raise ValueError(f"Conflicting market prices supplied for {key}.")
            normalized_prices[key] = price

        already_closed: list[PaperPosition] = []
        reasons: dict[str, str] = {}

        if self._ledger.emergency_stop_active:
            marked = self._ledger.mark_positions(normalized_prices, observed_at=observed_at)
            for position in self._ledger.open_positions:
                if position.symbol.upper() not in normalized_prices:
                    continue
                result = self._ledger.close_position(
                    position.proposal_id,
                    market_price=normalized_prices[position.symbol.upper()],
                    closed_at=observed_at,
                    fee_rate=self._fee_rate,
                    slippage_bps=self._slippage_bps,
                    close_reason="EMERGENCY_STOP",
                )
                if result.status == "CLOSED" and result.position is not None:
                    already_closed.append(result.position)
                    reasons[position.proposal_id] = "EMERGENCY_STOP"
            remaining = len(self._ledger.open_positions)
            status = "EMERGENCY_STOP_ACTIVE"
            reason = (
                "Emergency stop remains active; all positions with supplied prices were paper-closed."
                if remaining == 0
                else f"Emergency stop remains active; {remaining} position(s) await a price update before paper-close."
            )
            return ProtectionResult(
                status=status,
                reason=reason,
                marked_positions=marked,
                closed_positions=tuple(already_closed),
                close_reasons=reasons,
                emergency_stop_active=True,
            )

        # Recover a threshold-crossing mark left by a prior process before accepting newer prices.
        for position in self._ledger.open_positions:
            exit_reason = self._trigger_reason(position, position.mark_price)
            if exit_reason is not None:
                close_result = self._ledger.close_position(
                    position.proposal_id,
                    market_price=position.mark_price,
                    closed_at=position.mark_observed_at,
                    fee_rate=self._fee_rate,
                    slippage_bps=self._slippage_bps,
                    close_reason=exit_reason,
                )
                if close_result.status == "CLOSED" and close_result.position is not None:
                    already_closed.append(close_result.position)
                    reasons[position.proposal_id] = exit_reason
                continue
            # Recover a persisted mark if the previous process exited between mark
            # persistence and trailing-extrema update.
            if self._trailing_distance_pct is not None:
                self._ledger.advance_trailing_stop(
                    position.proposal_id,
                    updated_at=position.mark_observed_at,
                    trailing_distance_pct=self._trailing_distance_pct,
                    activation_profit_pct=self._trailing_activation_pct,
                )

        marked = self._ledger.mark_positions(normalized_prices, observed_at=observed_at)
        still_marked: list[PaperPosition] = []
        for position in marked:
            exit_reason = self._trigger_reason(position, position.mark_price)
            if exit_reason is not None:
                close_result = self._ledger.close_position(
                    position.proposal_id,
                    market_price=position.mark_price,
                    closed_at=observed_at,
                    fee_rate=self._fee_rate,
                    slippage_bps=self._slippage_bps,
                    close_reason=exit_reason,
                )
                if close_result.status == "CLOSED" and close_result.position is not None:
                    already_closed.append(close_result.position)
                    reasons[position.proposal_id] = exit_reason
                continue
            if self._trailing_distance_pct is not None:
                position = self._ledger.advance_trailing_stop(
                    position.proposal_id,
                    updated_at=observed_at,
                    trailing_distance_pct=self._trailing_distance_pct,
                    activation_profit_pct=self._trailing_activation_pct,
                ) or position
            still_marked.append(position)

        if already_closed:
            status = "PROTECTION_EXIT"
            reason = "One or more paper positions were closed by stop-loss or take-profit protection."
        else:
            status = "MARKET_UPDATED"
            reason = "Paper positions marked; no protection exit was triggered."
        return ProtectionResult(
            status=status,
            reason=reason,
            marked_positions=tuple(still_marked),
            closed_positions=tuple(already_closed),
            close_reasons=reasons,
            emergency_stop_active=False,
        )

    @staticmethod
    def _trigger_reason(position: PaperPosition, price: float) -> str | None:
        if position.side == "LONG":
            if price <= position.stop_loss:
                return "STOP_LOSS"
            if price >= position.take_profit:
                return "TAKE_PROFIT"
        else:
            if price >= position.stop_loss:
                return "STOP_LOSS"
            if price <= position.take_profit:
                return "TAKE_PROFIT"
        return None

    @staticmethod
    def _validate_finite(name: str, value: float) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number.")
