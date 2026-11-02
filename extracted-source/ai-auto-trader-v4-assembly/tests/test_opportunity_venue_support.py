import pytest

from backend.opportunity_coordinator import validate_opportunity_venue


@pytest.mark.parametrize(
    ("market_type", "venue"),
    [
        ("SPOT", "BINANCE_SPOT_TESTNET"),
        ("FUTURES", "BINANCE_USDM_TESTNET"),
    ],
)
def test_opportunity_engine_accepts_explicit_spot_and_futures_testnet_pairs(
    market_type, venue
):
    assert validate_opportunity_venue(market_type, venue) == (market_type, venue)


@pytest.mark.parametrize(
    ("market_type", "venue"),
    [
        ("SPOT", "BINANCE_USDM_TESTNET"),
        ("FUTURES", "BINANCE_SPOT_TESTNET"),
        ("SPOT", "BINANCE_SPOT"),
        ("FUTURES", "BINANCE_FUTURES"),
        ("MARGIN", "BINANCE_SPOT_TESTNET"),
    ],
)
def test_opportunity_engine_rejects_venue_market_type_mismatch(market_type, venue):
    with pytest.raises(ValueError):
        validate_opportunity_venue(market_type, venue)
