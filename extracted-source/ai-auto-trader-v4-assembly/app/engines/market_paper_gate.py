"""Fail-closed boundary from public market frames into paper-only orchestration.

This gate validates feed health and consistency before delegating to the existing
risk/orchestration path. It has no exchange-order interface and never authorizes
orders. The processor supplied by the caller must itself enforce signal and risk
checks (for example, ``TradingOrchestrator.process``).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Generic, TypeVar

from backend.market_worker import FeedStatus, MarketFrame

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class MarketGateResult(Generic[T]):
    status: str
    reason: str
    result: T | None = None
    orders_authorized: bool = False


class MarketPaperGate(Generic[T]):
    """Check market-frame integrity and freshness before paper processing."""

    def __init__(self, processor: Callable[[object], T], *, max_age_seconds: float = 90.0) -> None:
        if isinstance(max_age_seconds, bool) or not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be finite and positive")
        self._processor = processor
        self.max_age_seconds = max_age_seconds

    def process(self, frame: MarketFrame, request: object, *, now: float) -> MarketGateResult[T]:
        if isinstance(now, bool) or not math.isfinite(now) or now < 0:
            return self._blocked("replay/current time must be finite and non-negative")
        if frame.status is not FeedStatus.HEALTHY:
            return self._blocked(f"market feed is {frame.status.value}")
        if frame.orders_authorized:
            return self._blocked("market frame unexpectedly claims order authorization")
        market = getattr(request, "market", None)
        if market is None:
            return self._blocked("request is missing its market snapshot")
        if frame.symbol != getattr(market, "symbol", None):
            return self._blocked("market frame symbol does not match request market")
        if not frame.closed_candles:
            return self._blocked("market frame contains no closed candles")
        if frame.mark_price is None or not math.isfinite(frame.mark_price) or frame.mark_price <= 0:
            return self._blocked("market frame has no valid mark price")
        if not getattr(market, "is_fresh", False):
            return self._blocked("request market snapshot is marked stale")
        request_price = getattr(market, "mark_price", None)
        if not isinstance(request_price, (int, float)) or not math.isfinite(request_price) or request_price <= 0:
            return self._blocked("request market snapshot has an invalid mark price")
        if not math.isclose(frame.mark_price, request_price, rel_tol=1e-9, abs_tol=1e-9):
            return self._blocked("market frame mark price does not match request snapshot")
        checked_at = _timestamp(frame.checked_at)
        observed_at = _timestamp(frame.mark_observed_at)
        if checked_at is None or observed_at is None:
            return self._blocked("market timestamps must be timezone-aware")
        if checked_at > now or observed_at > now:
            return self._blocked("market frame or mark price is future-dated")
        if now - checked_at > self.max_age_seconds or now - observed_at > self.max_age_seconds:
            return self._blocked("market frame or mark price is stale")
        if not isinstance(getattr(market, "observed_at", None), (int, float)):
            return self._blocked("request market timestamp is invalid")
        if not math.isfinite(market.observed_at) or market.observed_at > now:
            return self._blocked("request market timestamp is future-dated or invalid")
        if now - market.observed_at > self.max_age_seconds:
            return self._blocked("request market snapshot is stale")
        processed = self._processor(request)
        status = getattr(processed, "status", str(processed))
        return MarketGateResult(str(status), "healthy market frame passed to paper processor", processed, False)

    @staticmethod
    def _blocked(reason: str) -> MarketGateResult:
        return MarketGateResult("MARKET_DATA_BLOCKED", reason, None, False)


def _timestamp(value: datetime | None) -> float | None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc).timestamp()
