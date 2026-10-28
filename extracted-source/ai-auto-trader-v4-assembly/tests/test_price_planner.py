from datetime import datetime, timedelta, timezone

import pytest

from app.engines.price_planner import PricePlanRequest, PricePlanningConfig, plan_prices
from backend.market_data_adapter import MarketCandle

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def candles(symbol="BTCUSDT", *, count=20, close_time=None):
    out = []
    end = close_time or NOW - timedelta(minutes=1)
    start = end - timedelta(minutes=count)
    price = 100.0
    for i in range(count):
        opened = start + timedelta(minutes=i)
        close = price + (0.2 if i % 2 else -0.1)
        high = max(price, close) + 1.0
        low = min(price, close) - 1.0
        out.append(MarketCandle(
            symbol=symbol, open_time=opened, close_time=opened + timedelta(minutes=1) - timedelta(milliseconds=1),
            open=price, high=high, low=low, close=close, volume=100.0,
            quote_volume=10000.0, trades=10, is_closed=True,
        ))
        price = close
    return tuple(out)


def request(direction="LONG", *, ref=101.0, observed_at=None, data=None, **kwargs):
    values = {
        "signal_id": "sig-1", "symbol": "BTCUSDT", "direction": direction,
        "reference_price": ref, "reference_price_at": observed_at or NOW,
        "candles": data if data is not None else candles(), "now": NOW,
        "expires_at": NOW + timedelta(minutes=5),
        "estimated_round_trip_cost_pct": 0.05, "tick_size": 0.1,
    }
    values.update(kwargs)
    return PricePlanRequest(**values)


def test_plans_long_prices_with_tick_alignment_and_positive_cost_adjusted_reward():
    result = plan_prices(request("LONG"))

    assert result.status == "READY"
    assert result.plan is not None
    assert result.plan.direction == "LONG"
    assert result.plan.stop_price < result.plan.entry_price < result.plan.target_price
    assert result.plan.stop_price / 0.1 == pytest.approx(round(result.plan.stop_price / 0.1))
    assert result.plan.target_price / 0.1 == pytest.approx(round(result.plan.target_price / 0.1))
    assert result.plan.trigger_price > result.plan.entry_price
    assert result.plan.invalidation_price == result.plan.stop_price
    assert result.orders_authorized is False


def test_plans_short_prices_with_correct_geometry():
    result = plan_prices(request("SHORT"))

    assert result.status == "READY"
    assert result.plan is not None
    assert result.plan.target_price < result.plan.entry_price < result.plan.stop_price
    assert result.plan.trigger_price < result.plan.entry_price
    assert result.plan.invalidation_price == result.plan.stop_price


def test_stale_reference_price_fails_closed_without_plan():
    result = plan_prices(request(observed_at=NOW - timedelta(seconds=90)))
    assert result.status == "NO_PLAN"
    assert result.plan is None
    assert "stale" in result.reason.lower()


def test_missing_or_unclosed_candles_fail_closed():
    open_candles = tuple(MarketCandle(
        symbol=c.symbol, open_time=c.open_time, close_time=c.close_time,
        open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume,
        quote_volume=c.quote_volume, trades=c.trades, is_closed=False,
    ) for c in candles())
    result = plan_prices(request(data=open_candles))
    assert result.status == "NO_PLAN"
    assert result.plan is None


def test_inconsistent_prices_and_too_high_cost_fail_closed():
    result = plan_prices(request(ref=0))
    assert result.status == "NO_PLAN"
    high_cost = plan_prices(request(estimated_round_trip_cost_pct=99))
    assert high_cost.status == "NO_PLAN"


def test_plan_cannot_use_candles_from_different_symbols_or_future_timestamps():
    other = candles("ETHUSDT")
    assert plan_prices(request(data=other)).status == "NO_PLAN"
    future = candles(close_time=NOW + timedelta(minutes=2))
    assert plan_prices(request(data=future)).status == "NO_PLAN"


def test_invalid_request_expiry_and_bad_tick_size_raise():
    with pytest.raises(ValueError):
        plan_prices(request(expires_at=NOW))
    with pytest.raises(ValueError):
        plan_prices(request(tick_size=0))


def test_rest_open_candle_is_ignored_when_sufficient_closed_history_exists():
    history = candles()
    current_open = MarketCandle(
        symbol="BTCUSDT", open_time=NOW - timedelta(seconds=30), close_time=NOW + timedelta(seconds=29),
        open=101, high=102, low=100, close=101, volume=12,
        quote_volume=1200, trades=20, is_closed=False,
    )
    result = plan_prices(request(data=history + (current_open,)))
    assert result.status == "READY"
    assert result.plan is not None


def test_stale_closed_history_and_insufficient_atr_history_fail_closed():
    old_history = candles(close_time=NOW - timedelta(hours=1))
    assert plan_prices(request(data=old_history)).status == "NO_PLAN"
    short_history = candles(count=5)
    assert plan_prices(request(data=short_history)).status == "NO_PLAN"


def test_planned_prices_feed_the_existing_signal_handoff_contract():
    from app.engines.research_pipeline import ResearchCandidate
    from app.engines.signal_handoff import prepare_signal
    from backend.strategy_research import ResearchResult, StrategyAssessment

    priced = plan_prices(request())
    assert priced.status == "READY" and priced.plan is not None
    research = ResearchResult(
        symbol="BTCUSDT", decision="REVIEW_LONG", assessments=(
            StrategyAssessment("trend_following", "LONG", 80, ("trend",)),
            StrategyAssessment("momentum", "LONG", 75, ("momentum",)),
            StrategyAssessment("breakout", "LONG", 70, ("breakout",)),
        ), reasons=("agreement",),
    )
    candidate = ResearchCandidate(
        symbol="BTCUSDT", scanner_score=90, status="REVIEW", direction="LONG",
        reasons=("research aligned",), opportunity=None, strategy_research=research,
        liquidity=None,
    )
    handoff = prepare_signal(candidate, priced.plan, now=NOW)
    assert handoff.status == "VALID"
    assert handoff.watch_request is not None
    assert handoff.orders_authorized is False
