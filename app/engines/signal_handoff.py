"""Explicit bridge from research-only candidates to validated, watchable signals.

Entry/stop/target and trigger levels must be supplied by a separate price-planning
step. This module never derives those prices from research scores and never places
orders. A VALID result still requires a watcher trigger and independent risk review.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.engines.research_pipeline import ResearchCandidate
from backend.signal_validation import (
    SignalProposal,
    SignalValidationConfig,
    SignalValidationResult,
    StrategyVote,
    validate_signal,
)
from backend.signal_watcher import WatchRequest


@dataclass(frozen=True, slots=True)
class SignalPricePlan:
    """Explicit prices/timing from a separate, auditable price-planning step."""

    signal_id: str
    direction: Literal["LONG", "SHORT"]
    created_at: datetime
    expires_at: datetime
    market_data_at: datetime
    entry_price: float
    stop_price: float
    target_price: float
    estimated_round_trip_cost_pct: float
    trigger_price: float
    trigger_rule: str
    invalidation_price: float | None = None


@dataclass(frozen=True, slots=True)
class SignalHandoffResult:
    status: str
    reason: str
    signal: SignalProposal | None
    validation: SignalValidationResult | None
    watch_request: WatchRequest | None
    orders_authorized: bool = False


def prepare_signal(
    candidate: ResearchCandidate,
    plan: SignalPricePlan,
    *,
    now: datetime,
    validation_config: SignalValidationConfig | None = None,
    previously_seen_ids: set[str] | frozenset[str] = frozenset(),
) -> SignalHandoffResult:
    """Validate a research candidate using explicit prices and strategy evidence."""
    if candidate.status != "REVIEW" or candidate.direction not in ("LONG", "SHORT"):
        return _reject("candidate is not approved for research review")
    if candidate.orders_authorized:
        return _reject("research candidate unexpectedly claims order authorization")
    if candidate.symbol.strip().upper() != candidate.symbol or not candidate.symbol.strip():
        return _reject("candidate symbol must be non-empty and normalized")
    if candidate.direction != plan.direction:
        return _reject("explicit price-plan direction conflicts with research direction")
    research = candidate.strategy_research
    if research is None or research.symbol.strip().upper() != candidate.symbol:
        return _reject("strategy research evidence is missing or mismatched")
    if research.decision != f"REVIEW_{candidate.direction}" or not research.assessments:
        return _reject("strategy research does not confirm the candidate direction")

    votes = tuple(
        StrategyVote(
            strategy=assessment.strategy,
            direction=assessment.direction,
            strength=assessment.confidence_score,
        )
        for assessment in research.assessments
    )
    signal = SignalProposal(
        signal_id=plan.signal_id,
        symbol=candidate.symbol,
        direction=plan.direction,
        created_at=plan.created_at,
        expires_at=plan.expires_at,
        market_data_at=plan.market_data_at,
        entry_price=plan.entry_price,
        stop_price=plan.stop_price,
        target_price=plan.target_price,
        estimated_round_trip_cost_pct=plan.estimated_round_trip_cost_pct,
        votes=votes,
    )
    validation = validate_signal(
        signal,
        now=now,
        config=validation_config,
        previously_seen_ids=previously_seen_ids,
    )
    watch_request = None
    if validation.status == "VALID":
        try:
            watch_request = WatchRequest(
                watch_id=f"watch:{plan.signal_id}",
                signal_id=plan.signal_id,
                symbol=candidate.symbol,
                direction=plan.direction,
                trigger_price=plan.trigger_price,
                trigger_rule=plan.trigger_rule,
                created_at=plan.created_at,
                expires_at=plan.expires_at,
                invalidation_price=plan.invalidation_price,
            )
        except (TypeError, ValueError) as exc:
            return SignalHandoffResult(
                status="REJECT",
                reason=f"watch request invalid: {type(exc).__name__}",
                signal=signal,
                validation=validation,
                watch_request=None,
                orders_authorized=False,
            )
    return SignalHandoffResult(
        status=validation.status,
        reason="; ".join(validation.reasons),
        signal=signal,
        validation=validation,
        watch_request=watch_request,
        orders_authorized=False,
    )


def _reject(reason: str) -> SignalHandoffResult:
    return SignalHandoffResult(
        status="REJECT", reason=reason, signal=None, validation=None,
        watch_request=None, orders_authorized=False,
    )
