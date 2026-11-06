from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from backend.dataset_lineage import (
    DatasetProvenance,
    assert_independent_dataset,
    candle_dataset_sha256,
)
from backend.strategy_research import Candle

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def provenance(*, provider="binance-public", venue="futures-testnet", symbol="BTCUSDT",
               timeframe="1m", start=START, end=None, digest="a" * 64):
    return DatasetProvenance(
        provider=provider, venue=venue, symbol=symbol, timeframe=timeframe,
        window_start=start, window_end=end or start + timedelta(hours=1),
        candle_count=61, data_sha256=digest,
    )


def test_same_candle_content_is_rejected_even_when_metadata_is_relabeled():
    first = provenance()
    relabeled = provenance(provider="another-provider", venue="spot", digest="a" * 64)

    with pytest.raises(ValueError, match="content is reused"):
        assert_independent_dataset(relabeled, first)


def test_overlapping_windows_are_rejected_when_source_provider_is_relabeled():
    first = provenance(
        provider="binance-public", venue="futures-testnet",
        start=START, end=START + timedelta(hours=2), digest="a" * 64,
    )
    relabeled_source = provenance(
        provider="another-public-source", venue="futures-testnet",
        start=START + timedelta(hours=1), end=START + timedelta(hours=3),
        digest="b" * 64,
    )

    with pytest.raises(ValueError, match="windows overlap"):
        assert_independent_dataset(relabeled_source, first)


def test_overlapping_windows_are_rejected_for_same_market_scope():
    first = provenance(start=START, end=START + timedelta(hours=2))
    second = provenance(start=START + timedelta(hours=1), end=START + timedelta(hours=3),
                        digest="b" * 64)

    with pytest.raises(ValueError, match="windows overlap"):
        assert_independent_dataset(second, first)


def test_non_overlapping_windows_with_distinct_content_are_accepted():
    first = provenance(start=START, end=START + timedelta(hours=1))
    second = provenance(start=START + timedelta(hours=1, seconds=1),
                        end=START + timedelta(hours=2), digest="b" * 64)

    assert_independent_dataset(second, first) is None


def test_different_symbols_are_not_rejected_for_timestamp_overlap():
    first = provenance(symbol="BTCUSDT")
    second = provenance(symbol="ETHUSDT", digest="b" * 64)

    assert_independent_dataset(second, first) is None


def test_candle_hash_is_stable_and_changes_when_any_market_value_changes():
    rows = [
        Candle("BTCUSDT", START, 100.0, 101.0, 99.0, 100.5, 12.0),
        Candle("BTCUSDT", START + timedelta(minutes=1), 100.5, 102.0, 100.0, 101.5, 14.0),
    ]
    changed = [rows[0], replace(rows[1], volume=15.0)]

    assert candle_dataset_sha256(rows) == candle_dataset_sha256(list(rows))
    assert candle_dataset_sha256(rows) != candle_dataset_sha256(changed)


def test_candle_hash_rejects_duplicate_or_out_of_order_timestamps():
    row = Candle("BTCUSDT", START, 100.0, 101.0, 99.0, 100.5, 12.0)

    with pytest.raises(ValueError, match="strictly increasing"):
        candle_dataset_sha256([row, row])
