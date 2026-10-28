"""In-memory signal watcher for price/condition alerts; never places orders.

This module is a deterministic core for a future streaming worker. Persistence,
exchange feeds, push delivery, and execution are intentionally out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math


TRIGGER_RULES = {
    "AT_OR_ABOVE",
    "AT_OR_BELOW",
    "CANDLE_CLOSE_ABOVE",
    "CANDLE_CLOSE_BELOW",
}
_FINAL_STATUSES = {"TRIGGERED", "INVALIDATED", "EXPIRED"}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class WatchRequest:
    watch_id: str
    signal_id: str
    symbol: str
    direction: str
    trigger_price: float
    trigger_rule: str
    created_at: datetime
    expires_at: datetime
    invalidation_price: float | None = None

    def __post_init__(self) -> None:
        if not self.watch_id.strip() or not self.signal_id.strip() or not self.symbol.strip():
            raise ValueError("watch id, signal id, and symbol are required")
        if self.direction not in ("LONG", "SHORT"):
            raise ValueError("direction must be LONG or SHORT")
        if self.trigger_rule not in TRIGGER_RULES:
            raise ValueError("invalid trigger rule")
        if not math.isfinite(self.trigger_price) or self.trigger_price <= 0:
            raise ValueError("trigger price must be finite and positive")
        if self.invalidation_price is not None and (
            not math.isfinite(self.invalidation_price) or self.invalidation_price <= 0
        ):
            raise ValueError("invalidation price must be finite and positive")
        created, expires = _utc(self.created_at), _utc(self.expires_at)
        if expires <= created:
            raise ValueError("watch expiry must be later than creation")
        if self.invalidation_price is not None:
            if self.direction == "LONG" and self.invalidation_price >= self.trigger_price:
                raise ValueError("long invalidation must be below trigger price")
            if self.direction == "SHORT" and self.invalidation_price <= self.trigger_price:
                raise ValueError("short invalidation must be above trigger price")


@dataclass(frozen=True, slots=True)
class MarketTick:
    symbol: str
    price: float
    observed_at: datetime
    candle_close: float | None = None


@dataclass(frozen=True, slots=True)
class WatcherConfig:
    max_market_data_age_seconds: int = 10
    max_future_skew_seconds: int = 2

    def __post_init__(self) -> None:
        if self.max_market_data_age_seconds <= 0:
            raise ValueError("max market data age must be positive")
        if self.max_future_skew_seconds < 0:
            raise ValueError("future timestamp tolerance must be non-negative")


@dataclass(frozen=True, slots=True)
class WatchResult:
    watch_id: str
    signal_id: str
    symbol: str
    status: str
    reason: str
    notify: bool
    observed_price: float | None
    orders_authorized: bool = False


class SignalWatcher:
    """Evaluate active watches and emit at most one terminal notification each."""

    def __init__(self, config: WatcherConfig | None = None) -> None:
        self.config = config or WatcherConfig()
        self._requests: dict[str, WatchRequest] = {}
        self._statuses: dict[str, str] = {}
        self._notified: set[str] = set()

    def add(self, request: WatchRequest) -> None:
        if request.watch_id in self._requests:
            raise ValueError(f"duplicate watch id: {request.watch_id}")
        self._requests[request.watch_id] = request
        self._statuses[request.watch_id] = "WAITING"

    def get_status(self, watch_id: str) -> str:
        if watch_id not in self._statuses:
            raise KeyError(watch_id)
        return self._statuses[watch_id]

    def evaluate(self, tick: MarketTick, *, now: datetime) -> WatchResult:
        """Evaluate one active watch for a symbol; use evaluate_all for many."""
        request = self._requests_by_symbol(tick.symbol)
        return self._evaluate_request(request, tick, now=now)

    def evaluate_all(self, tick: MarketTick, *, now: datetime) -> tuple[WatchResult, ...]:
        """Evaluate every active watch for this symbol against the same fresh tick."""
        requests = [request for request in self._requests.values()
                    if request.symbol.upper() == tick.symbol.upper()
                    and self._statuses[request.watch_id] not in _FINAL_STATUSES]
        if not requests:
            raise ValueError(f"unknown active symbol for watch: {tick.symbol}")
        return tuple(self._evaluate_request(request, tick, now=now) for request in requests)

    def _evaluate_request(self, request: WatchRequest, tick: MarketTick, *, now: datetime) -> WatchResult:
        now_utc = _utc(now)
        observed = _utc(tick.observed_at)
        if not math.isfinite(tick.price) or tick.price <= 0:
            return self._make_result(request, "WAIT_RECHECK", "invalid market price", False, tick.price)
        if tick.candle_close is not None and (
            not math.isfinite(tick.candle_close) or tick.candle_close <= 0
        ):
            return self._make_result(request, "WAIT_RECHECK", "invalid candle close", False, tick.price)
        if observed > now_utc + timedelta(seconds=self.config.max_future_skew_seconds):
            return self._make_result(request, "WAIT_RECHECK", "market timestamp is in the future", False, tick.price)
        if (now_utc - observed).total_seconds() > self.config.max_market_data_age_seconds:
            return self._make_result(request, "WAIT_RECHECK", "market data is stale", False, tick.price)

        current = self._statuses[request.watch_id]
        if current in _FINAL_STATUSES:
            return self._make_result(request, current, "watch already reached a terminal state", False, tick.price)
        if now_utc >= _utc(request.expires_at):
            return self._terminal(request, "EXPIRED", "watch expired before trigger", tick.price)
        if request.invalidation_price is not None:
            invalid = (
                tick.price <= request.invalidation_price if request.direction == "LONG"
                else tick.price >= request.invalidation_price
            )
            if invalid:
                return self._terminal(request, "INVALIDATED", "price crossed the invalidation level", tick.price)

        if self._triggered(request, tick):
            return self._terminal(request, "TRIGGERED", "trigger condition reached; signal and risk must be revalidated", tick.price)
        return self._make_result(request, "WAITING", "waiting for trigger condition", False, tick.price)

    def _requests_by_symbol(self, symbol: str) -> WatchRequest:
        # A tick is intentionally evaluated against one explicit symbol per call.
        matches = [request for request in self._requests.values()
                   if request.symbol.upper() == symbol.upper()
                   and self._statuses[request.watch_id] not in _FINAL_STATUSES]
        if not matches:
            # Permit a terminal watch to return its terminal state without mutating it.
            matches = [request for request in self._requests.values() if request.symbol.upper() == symbol.upper()]
        if not matches:
            raise ValueError(f"unknown symbol for active watch: {symbol}")
        if len(matches) > 1:
            raise ValueError("multiple watches match this symbol; use evaluate_watch for explicit watch selection")
        return matches[0]

    @staticmethod
    def _triggered(request: WatchRequest, tick: MarketTick) -> bool:
        if request.trigger_rule == "AT_OR_ABOVE":
            return tick.price >= request.trigger_price
        if request.trigger_rule == "AT_OR_BELOW":
            return tick.price <= request.trigger_price
        if tick.candle_close is None:
            return False
        if request.trigger_rule == "CANDLE_CLOSE_ABOVE":
            return tick.candle_close > request.trigger_price
        return tick.candle_close < request.trigger_price

    def _terminal(self, request: WatchRequest, status: str, reason: str, price: float) -> WatchResult:
        self._statuses[request.watch_id] = status
        notify = request.watch_id not in self._notified
        self._notified.add(request.watch_id)
        return self._make_result(request, status, reason, notify, price)

    @staticmethod
    def _make_result(request: WatchRequest, status: str, reason: str,
                     notify: bool, price: float | None) -> WatchResult:
        return WatchResult(
            watch_id=request.watch_id,
            signal_id=request.signal_id,
            symbol=request.symbol,
            status=status,
            reason=reason,
            notify=notify,
            observed_price=price,
            orders_authorized=False,
        )
