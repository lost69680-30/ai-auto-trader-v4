from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.engines.research_pipeline import ResearchCandidate
from app.engines.signal_handoff import SignalPricePlan, prepare_signal
from backend.strategy_research import ResearchResult, StrategyAssessment

NOW = datetime(2026, 10, 10, 7, 0, tzinfo=timezone.utc)


def candidate(*, status="REVIEW", direction="LONG"):
    research = ResearchResult(
        symbol="BTCUSDT",
        decision="REVIEW_LONG" if direction == "LONG" else "REVIEW_SHORT",
        assessments=(
            StrategyAssessment("trend_following", direction, 80, ("trend aligned",)),
            StrategyAssessment("momentum", direction, 75, ("momentum aligned",)),
            StrategyAssessment("breakout", direction, 70, ("breakout aligned",)),
            StrategyAssessment("mean_reversion", "NEUTRAL", 0, ("neutral",)),
        ),
        reasons=("research agreement",),
    )
    return ResearchCandidate(
        symbol="BTCUSDT", scanner_score=90, status=status, direction=direction,
        reasons=("engines aligned",), opportunity=None,
        strategy_research=research, liquidity=None,
    )


def plan(*, direction="LONG", trigger_rule="AT_OR_ABOVE"):
    return SignalPricePlan(
        signal_id="signal-1", direction=direction, created_at=NOW, expires_at=NOW + timedelta(minutes=2),
        market_data_at=NOW, entry_price=100, stop_price=98, target_price=105,
        estimated_round_trip_cost_pct=0.1, trigger_price=100, trigger_rule=trigger_rule,
        invalidation_price=97 if direction == "LONG" else 103,
    )


def test_research_review_with_explicit_valid_price_plan_produces_validated_signal():
    result = prepare_signal(candidate(), plan(), now=NOW)

    assert result.status == "VALID"
    assert result.signal is not None
    assert result.signal.entry_price == 100
    assert result.validation.orders_authorized is False
    assert result.orders_authorized is False


def test_no_trade_research_candidate_cannot_become_signal():
    result = prepare_signal(candidate(status="NO_TRADE", direction="NONE"), plan(), now=NOW)

    assert result.status == "REJECT"
    assert result.signal is None
    assert result.orders_authorized is False


def test_signal_cannot_be_built_without_strategy_research_evidence():
    row = candidate()
    incomplete = ResearchCandidate(
        symbol=row.symbol, scanner_score=row.scanner_score, status=row.status,
        direction=row.direction, reasons=row.reasons, opportunity=None,
        strategy_research=None, liquidity=None,
    )
    result = prepare_signal(incomplete, plan(), now=NOW)

    assert result.status == "REJECT"
    assert result.signal is None


def test_direction_mismatch_between_research_and_explicit_price_plan_rejected():
    result = prepare_signal(candidate(direction="SHORT"), plan(direction="LONG"), now=NOW)

    assert result.status == "REJECT"
    assert result.signal is None


def test_expired_plan_is_rejected_by_validator():
    expired = SignalPricePlan(
        signal_id="old-signal", direction="LONG", created_at=NOW - timedelta(minutes=3),
        expires_at=NOW - timedelta(seconds=1), market_data_at=NOW,
        entry_price=100, stop_price=98, target_price=105,
        estimated_round_trip_cost_pct=0.1, trigger_price=100,
        trigger_rule="AT_OR_ABOVE", invalidation_price=97,
    )
    result = prepare_signal(candidate(), expired, now=NOW)

    assert result.status == "REJECT"
    assert result.validation.status == "REJECT"
    assert result.orders_authorized is False


def test_valid_signal_gets_watch_request_but_never_order_authorization():
    result = prepare_signal(candidate(), plan(), now=NOW)

    assert result.status == "VALID"
    assert result.watch_request is not None
    assert result.watch_request.signal_id == "signal-1"
    assert result.watch_request.trigger_price == 100
    assert result.orders_authorized is False


def test_invalid_trigger_rule_cannot_create_watch_request():
    result = prepare_signal(candidate(), plan(trigger_rule="MAGIC_TRIGGER"), now=NOW)

    assert result.status == "REJECT"
    assert result.watch_request is None
    assert result.orders_authorized is False
