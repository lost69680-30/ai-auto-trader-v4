from datetime import datetime, timedelta, timezone

from backend.opportunity_intelligence import OpportunityConfig, assess_opportunity
from backend.strategy_research import Candle


NOW = datetime(2026, 10, 10, 7, 0, tzinfo=timezone.utc)


def candles_for(prices, symbol="BTCUSDT"):
    rows = []
    for index, close in enumerate(prices):
        rows.append(Candle(
            symbol=symbol,
            timestamp=NOW - timedelta(seconds=(len(prices) - index - 1) * 60),
            open=close * 0.999,
            high=close * 1.002,
            low=close * 0.998,
            close=close,
            volume=1000.0,
        ))
    return rows


def test_report_is_research_only_and_notifies_downstream_agents():
    prices = [100 + i * 0.7 for i in range(40)]
    report = assess_opportunity("BTCUSDT", candles_for(prices), now=NOW)

    assert report.symbol == "BTCUSDT"
    assert report.opportunity_id
    assert report.orders_authorized is False
    assert {message.recipient for message in report.notifications} >= {
        "strategy_coordinator", "risk_guardian", "portfolio_coordinator", "decision_journal"
    }
    assert all(message.event_type == "opportunity.assessed" for message in report.notifications)


def test_insufficient_history_fails_closed_and_notifies_no_trade():
    report = assess_opportunity("BTCUSDT", candles_for([100, 101, 102]), now=NOW)
    assert report.status == "NO_TRADE"
    assert report.direction == "NONE"
    assert report.orders_authorized is False
    assert "insufficient" in " ".join(report.evidence).lower()


def test_flat_market_is_not_promoted_to_opportunity():
    report = assess_opportunity("ETHUSDT", candles_for([100.0] * 40, "ETHUSDT"), now=NOW)
    assert report.status == "NO_TRADE"
    assert report.direction == "NONE"


def test_invalid_config_is_rejected():
    try:
        OpportunityConfig(min_history=2)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid history threshold should be rejected")
