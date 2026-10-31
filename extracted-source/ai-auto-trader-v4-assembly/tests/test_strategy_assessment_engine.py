from datetime import datetime, timezone

from backend.market_assessment import MarketAssessment, TimeframeAssessment
from backend.opportunity_store import StrategyAssessmentRecord
from backend.strategy_assessment_engine import (
    DEFAULT_STRATEGY_PROFILES,
    MultiStrategyAssessmentEngine,
    StrategyCandidate,
    StrategyProfile,
)


NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


def market_assessment(*, missing=(), directions=None):
    directions = directions or {}
    timeframes = []
    for timeframe in ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M", "1Y"):
        direction = directions.get(timeframe, "BULLISH")
        timeframes.append(TimeframeAssessment(
            timeframe=timeframe,
            status="MISSING" if timeframe in missing else "HEALTHY",
            direction="INSUFFICIENT_DATA" if timeframe in missing else direction,
            trend_score=None if timeframe in missing else (80.0 if direction == "BULLISH" else 20.0),
            momentum_pct=0.8,
            volatility_pct=0.5,
            efficiency_ratio=0.5,
            relative_volume=1.2,
            structure="HIGHER_HIGHS_HIGHER_LOWS",
            candlestick_pattern="NONE",
            structure_event="NONE",
            support=99.0,
            resistance=101.0,
            latest_close_time=NOW,
            reasons=("missing timeframe",) if timeframe in missing else (),
            orders_authorized=False,
        ))
    return MarketAssessment(
        symbol="BTCUSDT",
        assessed_at=NOW,
        direction="BULLISH",
        market_regime="TRENDING",
        market_phase="TRENDING",
        long_term_bias="BULLISH",
        medium_term_bias="BULLISH",
        short_term_bias="BULLISH",
        conflicts=(),
        timeframes=tuple(timeframes),
        orders_authorized=False,
    )


def candidate(*, assessment=None, **overrides):
    values = dict(
        opportunity_id="opportunity-123",
        symbol="BTCUSDT",
        market_type="FUTURES",
        market_assessment=assessment or market_assessment(),
        spread_bps=3.0,
        quote_volume_24h=20_000_000.0,
        estimated_round_trip_cost_bps=10.0,
        assessment_version=1,
    )
    values.update(overrides)
    return StrategyCandidate(**values)


def record_for(profile, candidate, decision, direction):
    return StrategyAssessmentRecord(
        assessment_id=f"{candidate.opportunity_id}:{profile.strategy_id}:{candidate.assessment_version}",
        opportunity_id=candidate.opportunity_id,
        strategy_id=profile.strategy_id,
        strategy_version=profile.strategy_version,
        assessment_version=candidate.assessment_version,
        decision=decision,
        payload={
            "direction": direction,
            "orders_authorized": False,
            "calibration_status": "NOT_CALIBRATED",
            "eligible_for_risk_review": decision == "ELIGIBLE" and not profile.analysis_only,
            "analysis_only": profile.analysis_only,
            "paper_eligible": decision == "ELIGIBLE" and not profile.analysis_only,
        },
        evidence_refs=["snapshot:BTCUSDT:latest"],
        orders_authorized=False,
    )


def test_engine_returns_five_independent_versioned_strategy_assessments():
    engine = MultiStrategyAssessmentEngine()
    results = engine.assess(
        candidate(),
        ["snapshot:BTCUSDT:latest"],
        now=NOW,
    )

    assert {item.strategy_id for item in results} == {
        "scalping", "day", "swing", "position", "long_term"
    }
    assert len({item.assessment_id for item in results}) == 5
    assert all(item.assessment_version == 1 for item in results)
    assert all(item.orders_authorized is False for item in results)
    assert all(item.payload["calibration_status"] == "NOT_CALIBRATED" for item in results)
    assert all(item.payload["score_type"] == "HEURISTIC_FIT" for item in results)


def test_strategy_timeframe_profiles_reuse_existing_adaptive_strategy_requirements():
    profiles = {item.strategy_id: item for item in DEFAULT_STRATEGY_PROFILES}

    assert profiles["scalping"].required_timeframes == ("1m", "5m", "15m", "1h")
    assert profiles["day"].required_timeframes == ("5m", "15m", "1h", "4h")
    assert profiles["swing"].required_timeframes == ("1h", "4h", "1d", "1w")
    assert profiles["position"].required_timeframes == ("1d", "1w", "1M")
    assert profiles["long_term"].required_timeframes == ("1w", "1M", "1Y")


def test_rejection_from_one_strategy_does_not_erase_another_strategy_assessment():
    profiles = (
        StrategyProfile("scalping", "SCALPING", ("1m", "5m"), "1.0.0"),
        StrategyProfile("swing", "SWING_TRADING", ("1h", "4h"), "1.0.0"),
    )

    def reject(candidate, evidence, profile, now):
        return record_for(profile, candidate, "REJECT", "NONE")

    def approve(candidate, evidence, profile, now):
        return record_for(profile, candidate, "ELIGIBLE", "LONG")

    engine = MultiStrategyAssessmentEngine(
        profiles=profiles,
        assessors={"scalping": reject, "swing": approve},
    )
    results = engine.assess(candidate(), ["snapshot:BTCUSDT:latest"], now=NOW)

    assert [item.decision for item in results] == ["REJECT", "ELIGIBLE"]
    assert results[0].payload["direction"] == "NONE"
    assert results[1].payload["direction"] == "LONG"


def test_opposing_strategy_directions_are_preserved_without_majority_vote():
    profiles = (
        StrategyProfile("scalping", "SCALPING", ("1m", "5m"), "1.0.0"),
        StrategyProfile("swing", "SWING_TRADING", ("1h", "4h"), "1.0.0"),
    )

    def assess_long(candidate, evidence, profile, now):
        return record_for(profile, candidate, "ELIGIBLE", "LONG")

    def assess_short(candidate, evidence, profile, now):
        return record_for(profile, candidate, "ELIGIBLE", "SHORT")

    results = MultiStrategyAssessmentEngine(
        profiles=profiles,
        assessors={"scalping": assess_long, "swing": assess_short},
    ).assess(candidate(), [], now=NOW)

    assert [item.payload["direction"] for item in results] == ["LONG", "SHORT"]
    assert all(item.decision == "ELIGIBLE" for item in results)


def test_long_term_analysis_is_never_eligible_for_paper_handoff():
    profile = next(item for item in DEFAULT_STRATEGY_PROFILES if item.strategy_id == "long_term")
    result = MultiStrategyAssessmentEngine(profiles=(profile,)).assess(
        candidate(), ["snapshot:BTCUSDT:weekly"], now=NOW,
    )[0]

    assert result.decision == "ANALYSIS_ONLY"
    assert result.payload["analysis_only"] is True
    assert result.payload["eligible_for_risk_review"] is False
    assert result.payload["paper_eligible"] is False
    assert result.orders_authorized is False


def test_missing_required_timeframe_requests_more_evidence():
    profile = next(item for item in DEFAULT_STRATEGY_PROFILES if item.strategy_id == "scalping")
    result = MultiStrategyAssessmentEngine(profiles=(profile,)).assess(
        candidate(assessment=market_assessment(missing=("1m",))),
        ["snapshot:BTCUSDT:5m"],
        now=NOW,
    )[0]

    assert result.decision == "REQUEST_MORE_EVIDENCE"
    assert result.payload["eligible_for_risk_review"] is False
    assert any("1m" in reason for reason in result.payload["reasons"])


def test_assessor_failure_is_recorded_without_discarding_other_results():
    profiles = (
        StrategyProfile("scalping", "SCALPING", ("1m", "5m"), "1.0.0"),
        StrategyProfile("swing", "SWING_TRADING", ("1h", "4h"), "1.0.0"),
    )

    def fail(candidate, evidence, profile, now):
        raise RuntimeError("sensitive connector detail")

    def succeed(candidate, evidence, profile, now):
        return record_for(profile, candidate, "ELIGIBLE", "LONG")

    results = MultiStrategyAssessmentEngine(
        profiles=profiles,
        assessors={"scalping": fail, "swing": succeed},
    ).assess(candidate(), [], now=NOW)

    assert results[0].decision == "REQUEST_MORE_EVIDENCE"
    assert "RuntimeError" in " ".join(results[0].payload["reasons"])
    assert "sensitive connector detail" not in str(results[0].payload)
    assert results[1].decision == "ELIGIBLE"
