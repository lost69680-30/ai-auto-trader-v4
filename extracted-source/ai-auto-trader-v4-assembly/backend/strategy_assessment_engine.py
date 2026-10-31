"""Independent, research-only assessment of each Solo strategy horizon."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import math
from typing import Any, Iterable, Mapping, Protocol

from backend.adaptive_strategy import select_strategy
from backend.market_assessment import MarketAssessment
from backend.opportunity_store import StrategyAssessmentRecord
from backend.strategy_profiles import DEFAULT_STRATEGY_PROFILES, StrategyProfile


@dataclass(frozen=True, slots=True)
class StrategyCandidate:
    opportunity_id: str
    symbol: str
    market_type: str
    market_assessment: MarketAssessment
    spread_bps: float | None
    quote_volume_24h: float | None
    estimated_round_trip_cost_bps: float | None
    assessment_version: int = 1
    orders_authorized: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.opportunity_id, str) or not self.opportunity_id.strip():
            raise ValueError("opportunity_id is required")
        if not isinstance(self.symbol, str) or not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("symbol must be trimmed uppercase text")
        if self.market_type not in {"SPOT", "FUTURES"}:
            raise ValueError("market_type must be SPOT or FUTURES")
        if not isinstance(self.market_assessment, MarketAssessment):
            raise ValueError("market_assessment is required")
        if self.market_assessment.orders_authorized is not False:
            raise ValueError("market assessment must deny order authorization")
        if self.market_assessment.symbol != self.symbol:
            raise ValueError("candidate and market assessment symbols must match")
        if self.orders_authorized is not False:
            raise ValueError("orders_authorized must be false")
        if (
            isinstance(self.assessment_version, bool)
            or not isinstance(self.assessment_version, int)
            or self.assessment_version < 1
        ):
            raise ValueError("assessment_version must be a positive integer")
        for name in ("spread_bps", "quote_volume_24h", "estimated_round_trip_cost_bps"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0
            ):
                raise ValueError(f"{name} must be a finite non-negative number")


class StrategyAssessor(Protocol):
    def __call__(
        self,
        candidate: StrategyCandidate,
        evidence: tuple[str, ...],
        profile: StrategyProfile,
        now: datetime,
    ) -> StrategyAssessmentRecord: ...


def _paper_safe(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "orders_authorized" and item is not False:
                return False
            if not _paper_safe(item):
                return False
    elif isinstance(value, (list, tuple)):
        return all(_paper_safe(item) for item in value)
    return True


def _assessment_id(candidate: StrategyCandidate, profile: StrategyProfile) -> str:
    token = "|".join((
        candidate.opportunity_id,
        profile.strategy_id,
        profile.strategy_version,
        str(candidate.assessment_version),
    ))
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:40]


def _request_more_evidence(reasons: tuple[str, ...]) -> bool:
    markers = ("missing", "unhealthy", "insufficient", "evidence is missing")
    return any(marker in reason.lower() for reason in reasons for marker in markers)


def _adaptive_assessor(
    candidate: StrategyCandidate,
    evidence: tuple[str, ...],
    profile: StrategyProfile,
    now: datetime,
) -> StrategyAssessmentRecord:
    enabled = () if profile.analysis_only else (profile.adaptive_strategy_name,)
    selection = select_strategy(
        candidate.market_assessment,
        market_type=candidate.market_type,
        spread_bps=candidate.spread_bps,
        quote_volume_24h=candidate.quote_volume_24h,
        estimated_round_trip_cost_bps=candidate.estimated_round_trip_cost_bps,
        enabled_strategies=enabled,
    )
    rank = next(
        (item for item in selection.ranking if item.strategy == profile.adaptive_strategy_name),
        None,
    )
    if rank is None:
        decision = "REQUEST_MORE_EVIDENCE"
        reasons = ("strategy assessor did not return an independent assessment",)
        direction = "NONE"
        fit_score = None
        eligible_for_risk_review = False
    else:
        reasons = tuple(rank.reasons)
        direction = rank.direction
        fit_score = rank.fit_score
        if profile.analysis_only and rank.eligible:
            decision = "ANALYSIS_ONLY"
        elif rank.eligible and rank.eligible_for_risk_review:
            decision = "ELIGIBLE"
        elif _request_more_evidence(reasons):
            decision = "REQUEST_MORE_EVIDENCE"
        else:
            decision = "NO_TRADE"
        eligible_for_risk_review = (
            not profile.analysis_only
            and decision == "ELIGIBLE"
            and rank.eligible_for_risk_review
        )

    payload = {
        "strategy_id": profile.strategy_id,
        "strategy_version": profile.strategy_version,
        "direction": direction,
        "fit_score": fit_score,
        "score_type": "HEURISTIC_FIT",
        "calibration_status": "NOT_CALIBRATED",
        "required_timeframes": list(profile.required_timeframes),
        "reasons": list(reasons),
        "analysis_only": profile.analysis_only,
        "eligible_for_risk_review": eligible_for_risk_review,
        "paper_eligible": eligible_for_risk_review,
        "market_regime": candidate.market_assessment.market_regime,
        "assessed_at": now.isoformat(),
        "orders_authorized": False,
    }
    return StrategyAssessmentRecord(
        assessment_id=_assessment_id(candidate, profile),
        opportunity_id=candidate.opportunity_id,
        strategy_id=profile.strategy_id,
        strategy_version=profile.strategy_version,
        assessment_version=candidate.assessment_version,
        decision=decision,
        payload=payload,
        evidence_refs=list(evidence),
        orders_authorized=False,
    )


class MultiStrategyAssessmentEngine:
    """Run each strategy independently; never aggregate away disagreement."""

    def __init__(
        self,
        profiles: Iterable[StrategyProfile] = DEFAULT_STRATEGY_PROFILES,
        assessors: Mapping[str, StrategyAssessor] | None = None,
    ) -> None:
        self.profiles = tuple(profiles)
        if not self.profiles:
            raise ValueError("at least one strategy profile is required")
        strategy_ids = [profile.strategy_id for profile in self.profiles]
        if len(set(strategy_ids)) != len(strategy_ids):
            raise ValueError("strategy profiles must have unique strategy_id values")
        if any(not isinstance(profile, StrategyProfile) for profile in self.profiles):
            raise ValueError("profiles must contain StrategyProfile records")
        self.assessors = dict(assessors or {})
        if set(self.assessors).difference(strategy_ids):
            raise ValueError("assessors contains a strategy without a matching profile")

    @staticmethod
    def _failure_record(
        candidate: StrategyCandidate,
        profile: StrategyProfile,
        evidence: tuple[str, ...],
        now: datetime,
        reason: str,
    ) -> StrategyAssessmentRecord:
        return StrategyAssessmentRecord(
            assessment_id=_assessment_id(candidate, profile),
            opportunity_id=candidate.opportunity_id,
            strategy_id=profile.strategy_id,
            strategy_version=profile.strategy_version,
            assessment_version=candidate.assessment_version,
            decision="REQUEST_MORE_EVIDENCE",
            payload={
                "strategy_id": profile.strategy_id,
                "strategy_version": profile.strategy_version,
                "direction": "NONE",
                "fit_score": None,
                "score_type": "HEURISTIC_FIT",
                "calibration_status": "NOT_CALIBRATED",
                "required_timeframes": list(profile.required_timeframes),
                "reasons": [reason],
                "analysis_only": profile.analysis_only,
                "eligible_for_risk_review": False,
                "paper_eligible": False,
                "assessed_at": now.isoformat(),
                "orders_authorized": False,
            },
            evidence_refs=list(evidence),
            orders_authorized=False,
        )

    def assess(
        self,
        candidate: StrategyCandidate,
        evidence: Iterable[str],
        *,
        now: datetime,
    ) -> tuple[StrategyAssessmentRecord, ...]:
        if not isinstance(candidate, StrategyCandidate):
            raise ValueError("a validated StrategyCandidate is required")
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        stamp = now.astimezone(timezone.utc)
        refs = tuple(evidence)
        if any(not isinstance(ref, str) or not ref.strip() or len(ref) > 240 for ref in refs):
            raise ValueError("evidence references must be non-empty strings of at most 240 characters")

        results: list[StrategyAssessmentRecord] = []
        for profile in self.profiles:
            assessor = self.assessors.get(profile.strategy_id, _adaptive_assessor)
            try:
                result = assessor(candidate, refs, profile, stamp)
                if not isinstance(result, StrategyAssessmentRecord):
                    raise TypeError("assessor returned an invalid result type")
                if (
                    result.opportunity_id != candidate.opportunity_id
                    or result.strategy_id != profile.strategy_id
                    or result.strategy_version != profile.strategy_version
                    or result.assessment_version != candidate.assessment_version
                    or result.orders_authorized is not False
                    or not _paper_safe(result.payload)
                ):
                    raise ValueError("assessor result violated the strategy or paper-only contract")
                if profile.analysis_only and (
                    result.decision == "ELIGIBLE"
                    or result.payload.get("eligible_for_risk_review") is True
                    or result.payload.get("paper_eligible") is True
                ):
                    raise ValueError("long-term analysis cannot be eligible for paper handoff")
                results.append(result)
            except Exception as exc:
                results.append(self._failure_record(
                    candidate, profile, refs, stamp,
                    f"assessment failed closed ({type(exc).__name__})",
                ))
        return tuple(results)
