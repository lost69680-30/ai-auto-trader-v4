"""Canonical market-dataset identity and overlap guards for offline research.

A candidate fingerprint is not evidence of independent data. This module binds
research datasets to their actual candle content and market scope, and rejects
overlapping windows or reused candle content before independent evaluation.
It has no exchange, account, order, or promotion interface.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from collections.abc import Iterable


def _text(value: object, name: str, maximum: int = 120) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a trimmed non-empty string")
    return value


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class DatasetProvenance:
    """Immutable, content-addressed identity for one candle dataset window."""
    provider: str
    venue: str
    symbol: str
    timeframe: str
    window_start: datetime
    window_end: datetime
    candle_count: int
    data_sha256: str

    def __post_init__(self) -> None:
        for field in ("provider", "venue", "symbol", "timeframe"):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        start = _utc(self.window_start, "window_start")
        end = _utc(self.window_end, "window_end")
        if start > end:
            raise ValueError("dataset window_start must not be after window_end")
        object.__setattr__(self, "window_start", start)
        object.__setattr__(self, "window_end", end)
        if isinstance(self.candle_count, bool) or not isinstance(self.candle_count, int) or self.candle_count < 1:
            raise ValueError("candle_count must be a positive integer")
        digest = _text(self.data_sha256, "data_sha256", 64).lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("data_sha256 must be a 64-character SHA-256 hex digest")
        object.__setattr__(self, "data_sha256", digest)


def candle_dataset_sha256(candles: Iterable[object]) -> str:
    """Hash normalized symbol/time/OHLCV rows; order and duplicates remain significant."""
    rows = []
    previous = None
    for candle in candles:
        symbol = _text(getattr(candle, "symbol", None), "candle symbol", 40)
        timestamp = _utc(getattr(candle, "timestamp", None), "candle timestamp")
        if previous is not None and timestamp <= previous:
            raise ValueError("candle timestamps must be strictly increasing and unique")
        previous = timestamp
        values = []
        for name in ("open", "high", "low", "close", "volume"):
            value = getattr(candle, name, None)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(f"candle {name} must be finite")
            values.append(format(float(value), ".17g"))
        rows.append([symbol, timestamp.isoformat(timespec="microseconds"), *values])
    if not rows:
        raise ValueError("cannot fingerprint an empty candle dataset")
    encoded = json.dumps(rows, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def assert_independent_dataset(candidate: DatasetProvenance, reference: DatasetProvenance) -> None:
    """Raise unless candidate data is provably disjoint from the reference dataset.

    Same-scope overlapping time windows are rejected even if their hashes differ.
    Identical content hashes are rejected even if metadata has been relabeled.
    """
    if not isinstance(candidate, DatasetProvenance) or not isinstance(reference, DatasetProvenance):
        raise ValueError("both datasets must include validated DatasetProvenance")
    if candidate.data_sha256 == reference.data_sha256:
        raise ValueError("dataset content is reused; relabeling metadata does not create independence")
    same_scope = (
        candidate.provider.casefold(), candidate.venue.casefold(),
        candidate.symbol.casefold(), candidate.timeframe.casefold(),
    ) == (
        reference.provider.casefold(), reference.venue.casefold(),
        reference.symbol.casefold(), reference.timeframe.casefold(),
    )
    if same_scope and not (
        candidate.window_end < reference.window_start
        or reference.window_end < candidate.window_start
    ):
        raise ValueError("dataset windows overlap for the same provider, venue, symbol, and timeframe")
