from datetime import datetime, timedelta, timezone

from backend.signal_validation import (
    SignalProposal,
    SignalValidationConfig,
    StrategyVote,
    validate_signal,
)

NOW = datetime(2026, 10, 10, 7, 0, tzinfo=timezone.utc)


def proposal(**overrides):
    values = dict(
        signal_id="sig-001",
        symbol="BTCUSDT",
        direction="LONG",
        created_at=NOW - timedelta(seconds=5),
        expires_at=NOW + timedelta(seconds=30),
        market_data_at=NOW - timedelta(seconds=3),
        entry_price=100.0,
        stop_price=98.0,
        target_price=105.0,
        estimated_round_trip_cost_pct=0.10,
        votes=(
            StrategyVote("trend_following", "LONG", 75.0),
            StrategyVote("momentum", "LONG", 70.0),
            StrategyVote("breakout", "NEUTRAL", 0.0),
        ),
    )
    values.update(overrides)
    return SignalProposal(**values)


def test_valid_signal_is_review_only_and_never_authorizes_orders():
    result = validate_signal(proposal(), now=NOW)
    assert result.status == "VALID"
    assert result.orders_authorized is False
    assert result.net_reward_risk > 1.0


def test_expired_signal_is_rejected():
    result = validate_signal(proposal(expires_at=NOW - timedelta(seconds=1)), now=NOW)
    assert result.status == "REJECT"
    assert "expired" in " ".join(result.reasons).lower()


def test_stale_market_data_is_rejected():
    result = validate_signal(proposal(market_data_at=NOW - timedelta(minutes=10)), now=NOW)
    assert result.status == "REJECT"
    assert any("stale" in reason.lower() for reason in result.reasons)


def test_future_signal_timestamp_is_rejected():
    result = validate_signal(proposal(created_at=NOW + timedelta(seconds=30)), now=NOW)
    assert result.status == "REJECT"


def test_invalid_stop_geometry_is_rejected():
    result = validate_signal(proposal(stop_price=101.0), now=NOW)
    assert result.status == "REJECT"
    assert any("stop" in reason.lower() for reason in result.reasons)


def test_insufficient_net_reward_risk_is_rejected():
    result = validate_signal(proposal(target_price=101.0, estimated_round_trip_cost_pct=0.2), now=NOW)
    assert result.status == "REJECT"
    assert any("reward" in reason.lower() for reason in result.reasons)


def test_strategy_disagreement_returns_wait_recheck():
    votes = (
        StrategyVote("trend_following", "LONG", 75.0),
        StrategyVote("momentum", "SHORT", 72.0),
        StrategyVote("breakout", "NEUTRAL", 0.0),
    )
    result = validate_signal(proposal(votes=votes), now=NOW)
    assert result.status == "WAIT_RECHECK"


def test_insufficient_strong_votes_returns_wait_recheck():
    votes = (StrategyVote("trend_following", "LONG", 75.0),)
    result = validate_signal(proposal(votes=votes), now=NOW)
    assert result.status == "WAIT_RECHECK"


def test_duplicate_signal_id_is_rejected():
    result = validate_signal(proposal(), now=NOW, previously_seen_ids={"sig-001"})
    assert result.status == "REJECT"
    assert any("duplicate" in reason.lower() for reason in result.reasons)


def test_malformed_or_nonfinite_price_is_rejected():
    result = validate_signal(proposal(entry_price=float("nan")), now=NOW)
    assert result.status == "REJECT"


def test_naive_clock_is_rejected():
    try:
        validate_signal(proposal(), now=datetime(2026, 10, 10, 7, 0))
    except ValueError as exc:
        assert "timezone-aware" in str(exc)
    else:
        raise AssertionError("naive now must be rejected")


def test_custom_thresholds_are_validated():
    try:
        SignalValidationConfig(minimum_net_reward_risk=0)
    except ValueError:
        pass
    else:
        raise AssertionError("zero reward-risk threshold must be rejected")
