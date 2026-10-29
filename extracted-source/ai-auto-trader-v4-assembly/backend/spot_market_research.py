"""Spot-only, read-only market structure research coordinator.

This module deliberately stops before price planning, sizing, risk orchestration,
or paper order creation. It uses Spot OHLCV and book/ticker snapshots only; it
never requests Futures mark prices or funding rates.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import math
from typing import Protocol

from backend.liquidity_structure import (
    Candle,
    LiquidityConfig,
    RankedOpportunity,
    SpotMarketStructureInput,
    rank_spot_opportunities,
)
from backend.market_data_adapter import (
    MarketCandle,
    MarketUniverse,
    BinanceSpotMarketDataAdapter,
)
from backend.scanner import ScanResult, ScannerConfig, scan_candidates


INTERVAL_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800,
    "12h": 43200, "1d": 86400, "3d": 259200, "1w": 604800,
}


class SpotResearchSource(Protocol):
    def fetch_market_universe(self, *, now: datetime | None = None) -> MarketUniverse: ...
    def fetch_klines(
        self, symbol: str, *, interval: str = "1m", limit: int = 100,
        now: datetime | None = None,
    ) -> tuple[MarketCandle, ...]: ...


@dataclass(frozen=True, slots=True)
class SpotResearchConfig:
    symbols: tuple[str, ...] = ()
    interval: str = "5m"
    candle_limit: int = 100
    max_candle_age_seconds: float = 120.0
    lookback_candles: int = 4
    estimated_round_trip_fee_pct: float = 0.0
    scanner: ScannerConfig = ScannerConfig(required_contract_type="SPOT")
    structure: LiquidityConfig = LiquidityConfig()

    def __post_init__(self) -> None:
        if self.interval not in INTERVAL_SECONDS:
            raise ValueError("unsupported Spot research interval")
        if isinstance(self.candle_limit, bool) or not isinstance(self.candle_limit, int) or not 3 <= self.candle_limit <= 1500:
            raise ValueError("candle_limit must be an integer from 3 through 1500")
        if isinstance(self.max_candle_age_seconds, bool) or not math.isfinite(self.max_candle_age_seconds) or self.max_candle_age_seconds <= 0:
            raise ValueError("max_candle_age_seconds must be finite and positive")
        if isinstance(self.lookback_candles, bool) or not isinstance(self.lookback_candles, int) or self.lookback_candles < 2:
            raise ValueError("lookback_candles must be at least 2")
        if len(set(self.symbols)) != len(self.symbols) or any(
            not isinstance(symbol, str) or not symbol or symbol != symbol.strip().upper()
            for symbol in self.symbols
        ):
            raise ValueError("symbols must be unique, trimmed uppercase symbols")
        if not math.isfinite(self.estimated_round_trip_fee_pct) or self.estimated_round_trip_fee_pct < 0:
            raise ValueError("estimated_round_trip_fee_pct must be finite and non-negative")
        if self.scanner.required_contract_type != "SPOT":
            raise ValueError("Spot research requires scanner.required_contract_type='SPOT'")


@dataclass(frozen=True, slots=True)
class SpotResearchRejection:
    symbol: str
    reason: str


@dataclass(frozen=True, slots=True)
class SpotResearchReport:
    status: str
    observed_at: datetime
    market_scan: ScanResult
    opportunities: tuple[RankedOpportunity, ...]
    rejections: tuple[SpotResearchRejection, ...]
    orders_authorized: bool = False


def _closed_candles(
    symbol: str, rows: tuple[MarketCandle, ...], *, now: datetime, config: SpotResearchConfig
) -> tuple[MarketCandle, ...] | None:
    closed = tuple(row for row in rows if row.is_closed)
    if len(closed) < max(config.lookback_candles + 1, 3):
        return None
    interval = timedelta(seconds=INTERVAL_SECONDS[config.interval])
    previous = None
    for row in closed:
        if row.symbol != symbol or row.close_time <= row.open_time:
            return None
        if row.open_time.tzinfo is None or row.close_time.tzinfo is None:
            return None
        if row.close_time > now or row.close_time <= row.open_time:
            return None
        if previous is not None:
            if row.open_time <= previous.open_time or row.open_time - previous.open_time != interval:
                return None
        previous = row
    age = (now - closed[-1].close_time).total_seconds()
    if age < 0 or age > config.max_candle_age_seconds:
        return None
    return closed


class SpotMarketResearchService:
    """Produce review-only Spot structure candidates from public market data."""

    def __init__(
        self,
        source: SpotResearchSource | None = None,
        config: SpotResearchConfig | None = None,
    ) -> None:
        self.source = source or BinanceSpotMarketDataAdapter()
        self.config = config or SpotResearchConfig()

    def analyze_once(self, *, now: datetime | None = None) -> SpotResearchReport:
        if now is not None and (now.tzinfo is None or now.utcoffset() is None):
            raise ValueError("now must be timezone-aware")
        explicit_clock = now is not None
        requested_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        universe = (
            self.source.fetch_market_universe(now=requested_at)
            if explicit_clock else self.source.fetch_market_universe()
        )
        scan_now = requested_at if explicit_clock else universe.fetched_at.astimezone(timezone.utc)
        if universe.orders_authorized:
            raise ValueError("market source must not authorize orders")
        contracts = universe.contracts
        snapshots = universe.snapshots
        if self.config.symbols:
            allowed = set(self.config.symbols)
            contracts = tuple(c for c in contracts if c.symbol in allowed)
            snapshots = tuple(s for s in snapshots if s.symbol in allowed)
        scan = scan_candidates(
            contracts, snapshots, now=scan_now, config=self.config.scanner
        )
        contracts_by_symbol = {c.symbol: c for c in contracts}
        snapshots_by_symbol = {s.symbol: s for s in snapshots}
        opportunities: list[RankedOpportunity] = []
        rejections: list[SpotResearchRejection] = []
        for candidate in scan.candidates:
            contract = contracts_by_symbol.get(candidate.symbol)
            snapshot = snapshots_by_symbol.get(candidate.symbol)
            if contract is None or snapshot is None or contract.contract_type != "SPOT":
                rejections.append(SpotResearchRejection(candidate.symbol, "missing or non-Spot market metadata"))
                continue
            try:
                if explicit_clock:
                    rows = self.source.fetch_klines(
                        candidate.symbol, interval=self.config.interval,
                        limit=self.config.candle_limit, now=requested_at,
                    )
                else:
                    rows = self.source.fetch_klines(
                        candidate.symbol, interval=self.config.interval,
                        limit=self.config.candle_limit,
                    )
            except Exception as exc:
                rejections.append(SpotResearchRejection(
                    candidate.symbol, f"candle retrieval failed safely ({type(exc).__name__})"
                ))
                continue
            validation_now = requested_at if explicit_clock else datetime.now(timezone.utc)
            closed = _closed_candles(candidate.symbol, rows, now=validation_now, config=self.config)
            if closed is None:
                rejections.append(SpotResearchRejection(
                    candidate.symbol, "closed candles are insufficient, stale, gapped, or invalid"
                ))
                continue
            structure_rows = tuple(Candle(
                open=row.open, high=row.high, low=row.low, close=row.close, volume=row.volume
            ) for row in closed)
            lookback = structure_rows[-(self.config.lookback_candles + 1):-1]
            volume_baseline = sum(row.volume for row in lookback) / len(lookback)
            if volume_baseline <= 0:
                rejections.append(SpotResearchRejection(candidate.symbol, "volume baseline is zero"))
                continue
            reference_level = max(row.high for row in lookback)
            previous_close = structure_rows[-2].close
            # Spread is expressed in basis points; dividing by 100 converts bps to percent.
            # Fee estimate is explicit configuration, never inferred from exchange account state.
            cost_pct = snapshot.spread_bps / 100.0 + self.config.estimated_round_trip_fee_pct
            item = SpotMarketStructureInput(
                symbol=candidate.symbol,
                candles=structure_rows,
                timestamp=closed[-1].close_time,
                reference_level=reference_level,
                previous_close=previous_close,
                volume_baseline=volume_baseline,
                estimated_round_trip_cost_pct=cost_pct,
            )
            structure_config = replace(
                self.config.structure,
                lookback_candles=self.config.lookback_candles,
            )
            opportunities.extend(rank_spot_opportunities(
                [item], now=validation_now, config=structure_config
            ))
        opportunities.sort(key=lambda row: (-row.score, row.symbol))
        opportunities = [
            replace(row, rank=index)
            for index, row in enumerate(opportunities, start=1)
        ]
        final_observed_at = requested_at if explicit_clock else datetime.now(timezone.utc)
        status = "REVIEW_CANDIDATES" if opportunities else ("NO_REVIEW_CANDIDATES" if not rejections else "PARTIAL_DATA")
        return SpotResearchReport(
            status=status, observed_at=final_observed_at, market_scan=scan,
            opportunities=tuple(opportunities), rejections=tuple(sorted(
                rejections, key=lambda row: (row.symbol, row.reason)
            )), orders_authorized=False,
        )
