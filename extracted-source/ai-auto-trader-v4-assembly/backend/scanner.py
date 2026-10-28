"""Fail-closed market scanner for research candidates, never order placement.

The score is a transparent screening heuristic, not a forecast or probability of profit.
Every accepted market is labelled REVIEW and this module has no execution interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Iterable


@dataclass(frozen=True, slots=True)
class Contract:
    symbol: str
    status: str
    contract_type: str
    quote_asset: str
    # Exchange metadata is optional at the research layer, but any paper sizing
    # or execution adapter must fail closed when these values are absent.
    tick_size: float | None = None
    quantity_step: float | None = None
    min_quantity: float | None = None
    max_quantity: float | None = None
    min_notional: float | None = None


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    symbol: str
    observed_at: datetime
    last_price: float
    quote_volume_24h: float
    spread_bps: float
    volatility_pct: float


@dataclass(frozen=True, slots=True)
class ScannerConfig:
    max_data_age_seconds: int = 90
    min_quote_volume_24h: float = 1_000_000.0
    max_spread_bps: float = 10.0
    min_volatility_pct: float = 0.05
    max_volatility_pct: float = 15.0
    required_status: str = "TRADING"
    required_contract_type: str = "PERPETUAL"
    required_quote_asset: str = "USDT"

    def __post_init__(self) -> None:
        if self.max_data_age_seconds <= 0:
            raise ValueError("max_data_age_seconds must be positive")
        if not math.isfinite(self.min_quote_volume_24h) or self.min_quote_volume_24h < 0:
            raise ValueError("min_quote_volume_24h must be finite and non-negative")
        if not math.isfinite(self.max_spread_bps) or self.max_spread_bps < 0:
            raise ValueError("max_spread_bps must be finite and non-negative")
        if not (math.isfinite(self.min_volatility_pct) and math.isfinite(self.max_volatility_pct)):
            raise ValueError("volatility thresholds must be finite")
        if self.min_volatility_pct < 0 or self.max_volatility_pct < self.min_volatility_pct:
            raise ValueError("volatility thresholds are invalid")


@dataclass(frozen=True, slots=True)
class Candidate:
    symbol: str
    score: float
    decision: str = "REVIEW"
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Rejection:
    symbol: str
    reason: str


@dataclass(frozen=True, slots=True)
class ScanResult:
    candidates: tuple[Candidate, ...]
    rejections: tuple[Rejection, ...]
    orders_authorized: bool = False


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _score(snapshot: MarketSnapshot, config: ScannerConfig) -> float:
    """Return a stable 0..100 ranking heuristic for screening only."""
    # Liquidity reaches its cap at 100x the configured minimum.
    if config.min_quote_volume_24h > 0:
        liquidity = min(1.0, math.log10(max(snapshot.quote_volume_24h, config.min_quote_volume_24h)
                                       / config.min_quote_volume_24h) / 2.0)
    else:
        liquidity = min(1.0, math.log10(max(snapshot.quote_volume_24h, 1.0)) / 8.0)
    spread = 1.0 if config.max_spread_bps == 0 and snapshot.spread_bps == 0 else (
        max(0.0, 1.0 - snapshot.spread_bps / config.max_spread_bps)
        if config.max_spread_bps > 0 else 0.0
    )
    # Prefer volatility inside the accepted range, without treating it as direction.
    midpoint = (config.min_volatility_pct + config.max_volatility_pct) / 2.0
    half_range = max((config.max_volatility_pct - config.min_volatility_pct) / 2.0, 1e-9)
    volatility_fit = max(0.0, 1.0 - abs(snapshot.volatility_pct - midpoint) / half_range)
    return round(100.0 * (0.45 * liquidity + 0.35 * spread + 0.20 * volatility_fit), 4)


def scan_candidates(
    contracts: Iterable[Contract],
    snapshots: Iterable[MarketSnapshot],
    *,
    now: datetime,
    config: ScannerConfig | None = None,
) -> ScanResult:
    """Screen eligible perpetual markets; output is research-only, never a trade signal."""
    if not _aware(now):
        raise ValueError("now must be timezone-aware")
    config = config or ScannerConfig()
    now = now.astimezone(timezone.utc)
    contract_list = list(contracts)
    snapshot_list = list(snapshots)
    rejections: list[Rejection] = []

    contract_counts: dict[str, int] = {}
    for item in contract_list:
        contract_counts[item.symbol] = contract_counts.get(item.symbol, 0) + 1
    snapshot_counts: dict[str, int] = {}
    snapshot_by_symbol: dict[str, MarketSnapshot] = {}
    for item in snapshot_list:
        snapshot_counts[item.symbol] = snapshot_counts.get(item.symbol, 0) + 1
        snapshot_by_symbol[item.symbol] = item

    candidates: list[Candidate] = []
    for contract in contract_list:
        symbol = contract.symbol
        if contract_counts[symbol] > 1:
            rejections.append(Rejection(symbol, "duplicate contract symbol; rejected fail-closed"))
            continue
        if not symbol or contract.status != config.required_status:
            rejections.append(Rejection(symbol, "contract status or symbol is not eligible"))
            continue
        if contract.contract_type != config.required_contract_type:
            rejections.append(Rejection(symbol, "contract is not the required perpetual type"))
            continue
        if contract.quote_asset != config.required_quote_asset:
            rejections.append(Rejection(symbol, "quote asset does not match configured asset"))
            continue
        if snapshot_counts.get(symbol, 0) > 1:
            rejections.append(Rejection(symbol, "duplicate market snapshots; rejected fail-closed"))
            continue
        snapshot = snapshot_by_symbol.get(symbol)
        if snapshot is None:
            rejections.append(Rejection(symbol, "missing market snapshot"))
            continue
        if snapshot.symbol != symbol:
            rejections.append(Rejection(symbol, "market snapshot symbol mismatch"))
            continue
        if not _aware(snapshot.observed_at):
            rejections.append(Rejection(symbol, "market timestamp is not timezone-aware"))
            continue
        age = (now - snapshot.observed_at.astimezone(timezone.utc)).total_seconds()
        if age < 0:
            rejections.append(Rejection(symbol, "market snapshot timestamp is in the future"))
            continue
        if age > config.max_data_age_seconds:
            rejections.append(Rejection(symbol, "market snapshot is stale"))
            continue
        numeric_values = (snapshot.last_price, snapshot.quote_volume_24h,
                          snapshot.spread_bps, snapshot.volatility_pct)
        if not all(math.isfinite(value) for value in numeric_values):
            rejections.append(Rejection(symbol, "market data contains non-finite values"))
            continue
        if snapshot.last_price <= 0:
            rejections.append(Rejection(symbol, "last price must be positive"))
            continue
        if snapshot.quote_volume_24h < 0 or snapshot.spread_bps < 0 or snapshot.volatility_pct < 0:
            rejections.append(Rejection(symbol, "volume, spread, and volatility must be non-negative"))
            continue
        if snapshot.quote_volume_24h < config.min_quote_volume_24h:
            rejections.append(Rejection(symbol, "24-hour quote volume is below threshold"))
            continue
        if snapshot.spread_bps > config.max_spread_bps:
            rejections.append(Rejection(symbol, "spread exceeds threshold"))
            continue
        if not config.min_volatility_pct <= snapshot.volatility_pct <= config.max_volatility_pct:
            rejections.append(Rejection(symbol, "volatility is outside configured research range"))
            continue
        candidates.append(Candidate(symbol=symbol, score=_score(snapshot, config),
                                    reasons=("eligible market data", "screening score only; strategy review required")))

    candidates.sort(key=lambda item: (-item.score, item.symbol))
    rejections.sort(key=lambda item: (item.symbol, item.reason))
    return ScanResult(candidates=tuple(candidates), rejections=tuple(rejections), orders_authorized=False)
