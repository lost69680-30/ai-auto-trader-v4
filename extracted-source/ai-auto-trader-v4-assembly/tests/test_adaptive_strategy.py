from datetime import datetime, timedelta, timezone

from backend.market_data_adapter import MarketCandle
from backend.market_assessment import assess_market
from backend.adaptive_strategy import select_strategy


NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


def series(interval_seconds, *, count=60, direction=1):
    rows = []
    latest_open = NOW - timedelta(seconds=interval_seconds * (count + 1))
    for index in range(count):
        opened = latest_open + timedelta(seconds=interval_seconds * index)
        closed = opened + timedelta(seconds=interval_seconds) - timedelta(milliseconds=1)
        price = 100.0 + direction * index * 0.5
        rows.append(MarketCandle(
            symbol="BTCUSDT", open_time=opened, close_time=closed,
            open=price - 0.2, high=price + 1, low=price - 1,
            close=price, volume=100.0, quote_volume=price * 100,
            trades=10, is_closed=True,
        ))
    return tuple(rows)


def bullish_assessment():
    return assess_market(
        "BTCUSDT",
        {
            "1m": series(60), "5m": series(300), "15m": series(900),
            "30m": series(1800), "1h": series(3600), "4h": series(14400),
            "1d": series(86400), "1w": series(7 * 86400),
        },
        now=NOW,
    )


def test_adaptive_selector_chooses_one_eligible_strategy_with_cost_and_liquidity_evidence():
    result = select_strategy(
        bullish_assessment(),
        market_type="FUTURES",
        spread_bps=2.0,
        quote_volume_24h=10_000_000.0,
        estimated_round_trip_cost_bps=8.0,
    )

    assert result.decision == "SELECTED"
    assert result.selected_strategy in {"SCALPING", "DAY_TRADING", "SWING_TRADING"}
    assert result.direction == "LONG"
    assert result.execution_eligible is True
    assert result.orders_authorized is False
    assert len([item for item in result.ranking if item.eligible]) >= 1


def test_adaptive_selector_waits_when_cost_or_liquidity_inputs_are_missing():
    result = select_strategy(
        bullish_assessment(),
        market_type="FUTURES",
        spread_bps=None,
        quote_volume_24h=None,
        estimated_round_trip_cost_bps=None,
    )

    assert result.decision == "NO_TRADE"
    assert result.selected_strategy is None
    assert result.execution_eligible is False
    assert result.orders_authorized is False


def test_adaptive_selector_never_short_sells_spot_assets():
    bearish = assess_market(
        "BTCUSDT",
        {
            "5m": series(300, direction=-1),
            "15m": series(900, direction=-1),
            "1h": series(3600, direction=-1),
            "4h": series(14400, direction=-1),
            "1d": series(86400, direction=-1),
        },
        now=NOW,
    )
    result = select_strategy(
        bearish,
        market_type="SPOT",
        spread_bps=2.0,
        quote_volume_24h=10_000_000.0,
        estimated_round_trip_cost_bps=8.0,
    )

    assert result.decision == "NO_TRADE"
    assert result.direction in {"NONE", "SHORT"}
    assert result.execution_eligible is False
    assert result.orders_authorized is False


def test_long_term_analysis_is_never_marked_execution_eligible():
    long_term = assess_market(
        "BTCUSDT",
        {
            "1w": series(7 * 86400),
            "1M": series(30 * 86400),
            "1Y": series(365 * 86400, count=4),
        },
        now=NOW,
    )
    result = select_strategy(long_term, market_type="SPOT")

    assert result.decision in {"ANALYSIS_ONLY", "NO_TRADE"}
    if result.selected_strategy == "LONG_TERM_ANALYSIS":
        assert result.execution_eligible is False
    assert result.orders_authorized is False


def test_adaptive_selector_rejects_excessive_spread_and_low_liquidity():
    result = select_strategy(
        bullish_assessment(),
        market_type="FUTURES",
        spread_bps=100.0,
        quote_volume_24h=1000.0,
        estimated_round_trip_cost_bps=200.0,
    )

    assert result.decision == "NO_TRADE"
    assert result.selected_strategy is None
    assert result.orders_authorized is False
