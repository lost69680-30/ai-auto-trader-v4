from datetime import datetime, timedelta, timezone

import pytest

from backend.strategy_research import Candle
from backend.market_data_lineage import (
    MarketDataIdentity,
    assert_independent_market_data,
    build_market_data_manifest,
)

START = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)


def candles(start_index=0, count=4):
    rows = []
    for offset in range(count):
        index = start_index + offset
        price = 100.0 + index
        rows.append(Candle(
            "BTCUSDT",
            START + timedelta(minutes=index),
            price,
            price + 1.0,
            price - 1.0,
            price + 0.5,
            10.0 + index,
        ))
    return rows


def identity(source_id):
    return MarketDataIdentity(
        venue="BINANCE_USDM_TESTNET",
        market_type="FUTURES",
        symbol="BTCUSDT",
        interval="1m",
        source_id=source_id,
    )


def test_same_candles_with_a_different_source_label_are_rejected_by_digest():
    learning = build_market_data_manifest(candles(), identity("learning-source"))
    evaluation = build_market_data_manifest(candles(), identity("renamed-source"))

    assert learning.source_id != evaluation.source_id
    assert learning.sha256 == evaluation.sha256
    with pytest.raises(ValueError, match="identical market data"):
        assert_independent_market_data((learning,), (evaluation,))


def test_partially_overlapping_market_windows_are_rejected_even_with_different_digests():
    learning = build_market_data_manifest(candles(0, 4), identity("source-a"))
    evaluation = build_market_data_manifest(candles(2, 4), identity("source-b"))

    assert learning.sha256 != evaluation.sha256
    with pytest.raises(ValueError, match="overlap"):
        assert_independent_market_data((learning,), (evaluation,))


def test_disjoint_windows_for_the_same_market_are_independent():
    learning = build_market_data_manifest(candles(0, 4), identity("source-a"))
    evaluation = build_market_data_manifest(candles(4, 4), identity("source-b"))

    assert evaluation.start_timestamp > learning.end_timestamp
    assert_independent_market_data((learning,), (evaluation,))


def test_manifest_captures_market_identity_window_count_and_ohlcv_digest():
    manifest = build_market_data_manifest(candles(5, 4), identity("supabase:paper_ledger.market_candles"))

    assert manifest.venue == "BINANCE_USDM_TESTNET"
    assert manifest.market_type == "FUTURES"
    assert manifest.symbol == "BTCUSDT"
    assert manifest.interval == "1m"
    assert manifest.start_timestamp == START + timedelta(minutes=5)
    assert manifest.end_timestamp == START + timedelta(minutes=8)
    assert manifest.row_count == 4
    assert manifest.source_id == "supabase:paper_ledger.market_candles"
    assert len(manifest.sha256) == 64
    assert manifest.orders_authorized is False
