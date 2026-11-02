from datetime import datetime, timedelta, timezone

import pytest

from app.engines.paper_ledger import PaperPosition
from app.engines.portfolio_exposure import calculate_portfolio_exposure

NOW = datetime(2026, 11, 2, 18, 0, tzinfo=timezone.utc)


def position(symbol, side, quantity, mark_price, *, observed_at=None):
    return PaperPosition(
        proposal_id=f"paper-{symbol}-{side}-{quantity}",
        symbol=symbol,
        side=side,
        quantity=quantity,
        entry_price=mark_price,
        mark_price=mark_price,
        stop_loss=mark_price * 0.9,
        take_profit=mark_price * 1.1,
        opened_at=NOW.timestamp() - 60,
        mark_observed_at=(observed_at or NOW).timestamp(),
        status="OPEN",
        opening_fee=0.1,
        closing_fee=0.0,
        funding_pnl=0.0,
        realized_pnl=None,
        closed_at=None,
        close_price=None,
    )


def test_portfolio_exposure_reports_gross_net_and_asset_concentration():
    report = calculate_portfolio_exposure(
        [
            position("BTCUSDT", "LONG", 1.0, 100.0),
            position("ETHUSDT", "LONG", 2.0, 100.0),
            position("BTCUSDT", "SHORT", 0.5, 100.0),
        ],
        equity=1000.0,
        now=NOW,
    )

    assert report.status == "COMPLETE"
    assert report.position_count == 3
    assert report.asset_count == 2
    assert report.gross_exposure == 350.0
    assert report.net_exposure == 150.0
    assert report.gross_exposure_pct_equity == 35.0
    assert report.net_exposure_pct_equity == 15.0
    assert report.top_asset_share_pct == pytest.approx(200 / 350 * 100)
    assert report.concentration_hhi == pytest.approx((150 / 350) ** 2 + (200 / 350) ** 2)
    assert report.exposure_by_symbol[0].symbol == "BTCUSDT"
    assert report.orders_authorized is False


def test_portfolio_exposure_rejects_stale_marks_instead_of_assuming_zero():
    stale = NOW - timedelta(minutes=1)
    with pytest.raises(ValueError, match="stale"):
        calculate_portfolio_exposure(
            [position("BTCUSDT", "LONG", 1.0, 100.0, observed_at=stale)],
            equity=1000.0,
            now=NOW,
            max_mark_age_seconds=30,
        )


def test_portfolio_exposure_rejects_invalid_positions_and_equity():
    with pytest.raises(ValueError, match="side"):
        calculate_portfolio_exposure(
            [position("BTCUSDT", "BUY", 1.0, 100.0)],
            equity=1000.0,
            now=NOW,
        )
    with pytest.raises(ValueError, match="equity"):
        calculate_portfolio_exposure([], equity=0.0, now=NOW)


def test_empty_portfolio_has_undefined_concentration_not_fabricated_zero():
    report = calculate_portfolio_exposure([], equity=1000.0, now=NOW)

    assert report.status == "EMPTY"
    assert report.position_count == 0
    assert report.gross_exposure == 0.0
    assert report.net_exposure == 0.0
    assert report.top_asset_share_pct is None
    assert report.concentration_hhi is None
    assert report.orders_authorized is False
