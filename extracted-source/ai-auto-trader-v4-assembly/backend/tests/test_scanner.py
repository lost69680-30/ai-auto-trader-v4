from datetime import datetime, timedelta, timezone

import pytest

from backend.scanner import Contract, MarketSnapshot, ScannerConfig, scan_candidates

NOW = datetime(2026, 10, 10, 7, 0, tzinfo=timezone.utc)


def contract(symbol="BTCUSDT", status="TRADING", kind="PERPETUAL", quote="USDT"):
    return Contract(symbol=symbol, status=status, contract_type=kind, quote_asset=quote)


def snapshot(symbol="BTCUSDT", *, observed_at=NOW, price=100.0, quote_volume=5_000_000.0,
             spread_bps=3.0, volatility_pct=1.5):
    return MarketSnapshot(symbol=symbol, observed_at=observed_at, last_price=price,
                          quote_volume_24h=quote_volume, spread_bps=spread_bps,
                          volatility_pct=volatility_pct)


def test_eligible_market_is_research_candidate_not_trade():
    result = scan_candidates([contract()], [snapshot()], now=NOW)
    assert len(result.candidates) == 1
    assert result.candidates[0].decision == "REVIEW"
    assert result.candidates[0].symbol == "BTCUSDT"
    assert result.candidates[0].score > 0
    assert result.orders_authorized is False


@pytest.mark.parametrize("item", [
    contract(status="BREAK"),
    contract(kind="CURRENT_QUARTER"),
    contract(quote="BUSD"),
])
def test_ineligible_contract_is_rejected(item):
    result = scan_candidates([item], [snapshot(symbol=item.symbol)], now=NOW)
    assert not result.candidates
    assert result.rejections


def test_stale_market_data_is_rejected():
    result = scan_candidates([contract()], [snapshot(observed_at=NOW - timedelta(minutes=5))], now=NOW)
    assert not result.candidates
    assert "stale" in result.rejections[0].reason.lower()


@pytest.mark.parametrize("changes", [
    {"price": float("nan")},
    {"price": -1.0},
    {"quote_volume": -2.0},
    {"spread_bps": -1.0},
    {"volatility_pct": float("inf")},
])
def test_invalid_market_values_are_rejected(changes):
    values = dict(price=100.0, quote_volume=5_000_000.0, spread_bps=3.0, volatility_pct=1.5)
    values.update(changes)
    result = scan_candidates([contract()], [snapshot(**values)], now=NOW)
    assert not result.candidates
    assert result.rejections


def test_missing_snapshot_is_rejected():
    result = scan_candidates([contract()], [], now=NOW)
    assert not result.candidates
    assert "missing" in result.rejections[0].reason.lower()


def test_symbol_mismatch_does_not_silently_join():
    result = scan_candidates([contract("BTCUSDT")], [snapshot("ETHUSDT")], now=NOW)
    assert not result.candidates
    assert result.rejections


def test_duplicate_contracts_fail_closed():
    result = scan_candidates([contract(), contract()], [snapshot()], now=NOW)
    assert not result.candidates
    assert any("duplicate" in item.reason.lower() for item in result.rejections)


def test_results_are_deterministically_ranked():
    contracts = [contract("ETHUSDT"), contract("BTCUSDT")]
    snapshots = [snapshot("ETHUSDT", quote_volume=2_000_000), snapshot("BTCUSDT", quote_volume=8_000_000)]
    first = scan_candidates(contracts, snapshots, now=NOW)
    second = scan_candidates(contracts, snapshots, now=NOW)
    assert [(x.symbol, x.score) for x in first.candidates] == [(x.symbol, x.score) for x in second.candidates]


def test_naive_now_is_rejected():
    with pytest.raises(ValueError):
        scan_candidates([contract()], [snapshot()], now=datetime(2026, 10, 10))


def test_bad_config_is_rejected():
    with pytest.raises(ValueError):
        ScannerConfig(max_data_age_seconds=-1)
