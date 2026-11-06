"""Canonical, immutable market-data manifests for research independence checks.

The digest covers the ordered timestamped OHLCV rows only. Source labels and
caller-provided dataset fingerprints are metadata and cannot make reused candles
appear independent.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from typing import Any


_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _required_text(value: object, name: str, maximum: int = 160) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > maximum
    ):
        raise ValueError(f"{name} must be a trimmed non-empty string")
    return value


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class MarketDataIdentity:
    venue: str
    market_type: str
    symbol: str
    interval: str
    source_id: str

    def __post_init__(self) -> None:
        for field in ("venue", "market_type", "symbol", "interval", "source_id"):
            object.__setattr__(self, field, _required_text(getattr(self, field), field, 200))


@dataclass(frozen=True, slots=True)
class MarketDataManifest:
    venue: str
    market_type: str
    symbol: str
    interval: str
    start_timestamp: datetime
    end_timestamp: datetime
    row_count: int
    source_id: str
    sha256: str
    orders_authorized: bool = False

    def __post_init__(self) -> None:
        for field in ("venue", "market_type", "symbol", "interval", "source_id"):
            object.__setattr__(self, field, _required_text(getattr(self, field), field, 200))
        start = _utc(self.start_timestamp, "start_timestamp")
        end = _utc(self.end_timestamp, "end_timestamp")
        if start > end:
            raise ValueError("market-data manifest start must not be after end")
        object.__setattr__(self, "start_timestamp", start)
        object.__setattr__(self, "end_timestamp", end)
        if isinstance(self.row_count, bool) or not isinstance(self.row_count, int) or self.row_count < 1:
            raise ValueError("market-data manifest row_count must be a positive integer")
        if not isinstance(self.sha256, str) or not _HEX_SHA256.fullmatch(self.sha256):
            raise ValueError("market-data manifest sha256 must be a lowercase SHA-256 digest")
        if self.orders_authorized is not False:
            raise ValueError("orders_authorized must be false")

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "market_type": self.market_type,
            "symbol": self.symbol,
            "interval": self.interval,
            "start_timestamp": self.start_timestamp.isoformat(),
            "end_timestamp": self.end_timestamp.isoformat(),
            "row_count": self.row_count,
            "source_id": self.source_id,
            "sha256": self.sha256,
            "orders_authorized": False,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MarketDataManifest":
        if not isinstance(value, Mapping):
            raise ValueError("market_data_manifest must be an object")
        try:
            start = datetime.fromisoformat(str(value["start_timestamp"]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(value["end_timestamp"]).replace("Z", "+00:00"))
            return cls(
                venue=value["venue"],
                market_type=value["market_type"],
                symbol=value["symbol"],
                interval=value["interval"],
                start_timestamp=start,
                end_timestamp=end,
                row_count=value["row_count"],
                source_id=value["source_id"],
                sha256=value["sha256"],
                orders_authorized=value.get("orders_authorized", False),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("market_data_manifest is incomplete or invalid") from exc


def build_market_data_manifest(
    candles: Iterable[Any],
    identity: MarketDataIdentity,
) -> MarketDataManifest:
    """Hash the ordered timestamped OHLCV source rows into an immutable manifest."""
    if not isinstance(identity, MarketDataIdentity):
        raise ValueError("a validated MarketDataIdentity is required")
    rows = tuple(candles)
    if not rows:
        raise ValueError("market-data manifest requires at least one candle")

    canonical_rows: list[dict[str, Any]] = []
    previous: datetime | None = None
    for index, candle in enumerate(rows):
        if getattr(candle, "symbol", None) != identity.symbol:
            raise ValueError(f"market-data symbol mismatch at row {index}")
        stamp = _utc(getattr(candle, "timestamp", None), f"candle timestamp at row {index}")
        if previous is not None and stamp <= previous:
            raise ValueError("market-data candles must have strictly increasing timestamps")
        values = {}
        for name in ("open", "high", "low", "close", "volume"):
            value = getattr(candle, name, None)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(f"candle {name} at row {index} must be finite")
            values[name] = float(value)
        if min(values["open"], values["high"], values["low"], values["close"]) <= 0 or values["volume"] < 0:
            raise ValueError(f"candle values at row {index} are invalid")
        if values["high"] < max(values["open"], values["close"], values["low"]):
            raise ValueError(f"candle high at row {index} is inconsistent")
        if values["low"] > min(values["open"], values["close"], values["high"]):
            raise ValueError(f"candle low at row {index} is inconsistent")
        canonical_rows.append({
            "timestamp": stamp.isoformat(timespec="microseconds"),
            **values,
        })
        previous = stamp

    material = json.dumps(canonical_rows, sort_keys=True, separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return MarketDataManifest(
        venue=identity.venue,
        market_type=identity.market_type,
        symbol=identity.symbol,
        interval=identity.interval,
        start_timestamp=_utc(rows[0].timestamp, "first candle timestamp"),
        end_timestamp=_utc(rows[-1].timestamp, "last candle timestamp"),
        row_count=len(rows),
        source_id=identity.source_id,
        sha256=digest,
        orders_authorized=False,
    )


def assert_independent_market_data(
    learning_manifests: Iterable[MarketDataManifest],
    evaluation_manifests: Iterable[MarketDataManifest],
) -> None:
    """Reject exact candle reuse and any overlapping symbol/timeframe windows."""
    learning = tuple(learning_manifests)
    evaluation = tuple(evaluation_manifests)
    if not learning or not evaluation:
        raise ValueError("learning and independent evaluation manifests are required")
    if any(not isinstance(item, MarketDataManifest) or item.orders_authorized is not False for item in (*learning, *evaluation)):
        raise ValueError("all market-data manifests must be validated and paper-only")

    learning_digests = {item.sha256 for item in learning}
    if any(item.sha256 in learning_digests for item in evaluation):
        raise ValueError("independent evaluation reuses identical market data")

    for training in learning:
        for held_out in evaluation:
            same_series = (
                training.symbol == held_out.symbol
                and training.interval == held_out.interval
            )
            overlaps = (
                held_out.start_timestamp <= training.end_timestamp
                and held_out.end_timestamp >= training.start_timestamp
            )
            if same_series and overlaps:
                raise ValueError("independent evaluation market-data window overlaps learning data")
