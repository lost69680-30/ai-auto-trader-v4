"""Rate-limited, fail-closed polling worker for paper-trading market data.

The worker is deliberately read-only: it consumes public REST data, exposes feed
health, and emits market frames to a caller-provided callback. It has no order
submission or strategy-execution authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import logging
import math
import threading
import time
from typing import Callable, Protocol

from backend.market_data_adapter import MarketCandle, MarketDataError, MarkPriceSnapshot


logger = logging.getLogger(__name__)


INTERVAL_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600,
    "8h": 28800, "12h": 43200, "1d": 86400, "3d": 259200,
    "1w": 604800,
}


class FeedStatus(str, Enum):
    HEALTHY = "HEALTHY"
    STALE = "STALE"
    GAP_DETECTED = "GAP_DETECTED"
    DISCONNECTED = "DISCONNECTED"
    NO_CLOSED_CANDLES = "NO_CLOSED_CANDLES"
    INVALID_DATA = "INVALID_DATA"


@dataclass(frozen=True, slots=True)
class MarketWorkerConfig:
    symbols: tuple[str, ...]
    interval: str = "1m"
    candle_limit: int = 100
    poll_interval_seconds: float = 5.0
    max_market_age_seconds: float = 90.0
    max_retries: int = 5
    retry_backoff_seconds: float = 1.0
    max_retry_backoff_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.symbols or any(not isinstance(s, str) or not s or s != s.upper().strip() for s in self.symbols):
            raise ValueError("symbols must be a non-empty tuple of trimmed uppercase symbols")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols must be unique")
        if self.interval not in INTERVAL_SECONDS:
            raise ValueError("unsupported interval for candle-gap monitoring")
        if isinstance(self.candle_limit, bool) or not isinstance(self.candle_limit, int) or not 2 <= self.candle_limit <= 1500:
            raise ValueError("candle_limit must be an integer from 2 through 1500")
        if not math.isfinite(self.poll_interval_seconds) or self.poll_interval_seconds < 1.0:
            raise ValueError("poll_interval_seconds must be finite and at least 1 second")
        if not math.isfinite(self.max_market_age_seconds) or self.max_market_age_seconds <= 0:
            raise ValueError("max_market_age_seconds must be finite and positive")
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int) or self.max_retries < 1:
            raise ValueError("max_retries must be a positive integer")
        if not math.isfinite(self.retry_backoff_seconds) or self.retry_backoff_seconds <= 0:
            raise ValueError("retry_backoff_seconds must be finite and positive")
        if not math.isfinite(self.max_retry_backoff_seconds) or self.max_retry_backoff_seconds < self.retry_backoff_seconds:
            raise ValueError("max_retry_backoff_seconds must be >= retry_backoff_seconds")


class PublicMarketDataSource(Protocol):
    def fetch_klines(self, symbol: str, *, interval: str, limit: int, now: datetime | None = None) -> tuple[MarketCandle, ...]: ...
    def fetch_mark_price(self, symbol: str) -> MarkPriceSnapshot: ...


@dataclass(frozen=True, slots=True)
class MarketFrame:
    symbol: str
    status: FeedStatus
    checked_at: datetime
    mark_price: float | None
    mark_observed_at: datetime | None
    closed_candles: tuple[MarketCandle, ...]
    reasons: tuple[str, ...]
    retry_count: int
    orders_authorized: bool = False
    funding_rate: float | None = None
    index_price: float | None = None
    next_funding_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MarketPollResult:
    checked_at: datetime
    frames: tuple[MarketFrame, ...]
    orders_authorized: bool = False

    @property
    def healthy_symbols(self) -> tuple[str, ...]:
        return tuple(frame.symbol for frame in self.frames if frame.status is FeedStatus.HEALTHY)

    @property
    def unhealthy_symbols(self) -> tuple[str, ...]:
        return tuple(frame.symbol for frame in self.frames if frame.status is not FeedStatus.HEALTHY)


class MarketDataWorker:
    """Poll public market data, reject stale/gapped feeds, and retry with backoff."""

    def __init__(self, source: PublicMarketDataSource, config: MarketWorkerConfig) -> None:
        self.source = source
        self.config = config
        self._retry_counts = {symbol: 0 for symbol in config.symbols}
        self._consecutive_failed_cycles = 0

    @property
    def next_delay_seconds(self) -> float:
        if self._consecutive_failed_cycles == 0:
            return self.config.poll_interval_seconds
        exponent = min(self._consecutive_failed_cycles - 1, self.config.max_retries - 1)
        return min(self.config.retry_backoff_seconds * (2 ** exponent), self.config.max_retry_backoff_seconds)

    def poll_once(self, *, now: datetime | None = None) -> MarketPollResult:
        # Keep a deterministic reference when tests/callers explicitly provide
        # `now`. In production, refresh validation time after network I/O so a
        # receive-time timestamp cannot look future-dated merely because the
        # poll began before the HTTP request completed.
        explicit_now = now is not None
        checked_at = now or datetime.now(timezone.utc)
        if checked_at.tzinfo is None or checked_at.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        checked_at = checked_at.astimezone(timezone.utc)
        frames = tuple(
            self._poll_symbol(symbol, checked_at, refresh_validation_time=not explicit_now)
            for symbol in self.config.symbols
        )
        if any(frame.status is FeedStatus.DISCONNECTED for frame in frames):
            self._consecutive_failed_cycles += 1
        else:
            self._consecutive_failed_cycles = 0
        return MarketPollResult(checked_at, frames, orders_authorized=False)

    def _poll_symbol(
        self, symbol: str, now: datetime, *, refresh_validation_time: bool = False
    ) -> MarketFrame:
        try:
            if refresh_validation_time:
                # Let the adapter timestamp the response at receipt time. Passing
                # the poll-start clock can falsely reject a new candle if the HTTP
                # request crosses a candle boundary.
                candles = self.source.fetch_klines(
                    symbol, interval=self.config.interval, limit=self.config.candle_limit
                )
            else:
                candles = self.source.fetch_klines(
                    symbol, interval=self.config.interval, limit=self.config.candle_limit, now=now
                )
            mark = self.source.fetch_mark_price(symbol)
        except Exception as exc:
            self._retry_counts[symbol] += 1
            # MarketDataError messages are authored by our adapter and contain only
            # endpoint paths / HTTP status details, never request headers or secrets.
            # Keep unexpected exception text hidden to avoid leaking arbitrary data.
            if isinstance(exc, MarketDataError):
                detail = f"{type(exc).__name__}: {exc}"
            else:
                detail = type(exc).__name__
            return self._frame(symbol, FeedStatus.DISCONNECTED, now, None, None, (),
                               (f"public market-data request failed: {detail}",))

        validation_now = datetime.now(timezone.utc) if refresh_validation_time else now
        reasons: list[str] = []
        if mark.symbol != symbol or not math.isfinite(mark.mark_price) or mark.mark_price <= 0:
            reasons.append("mark-price payload does not match symbol or contains an invalid price")
        if mark.orders_authorized is not False:
            reasons.append("market-data snapshot has unexpected order authority")

        # Spot adapters explicitly omit Futures-only fields. If any Futures
        # field is supplied, require a complete, valid context before forwarding.
        futures_context = (mark.index_price, mark.last_funding_rate, mark.next_funding_at)
        if any(value is not None for value in futures_context) and not all(
            value is not None for value in futures_context
        ):
            reasons.append("Futures context is incomplete")
        elif all(value is not None for value in futures_context):
            if (isinstance(mark.index_price, bool)
                    or not isinstance(mark.index_price, (int, float))
                    or not math.isfinite(mark.index_price) or mark.index_price <= 0):
                reasons.append("index price is invalid")
            if (isinstance(mark.last_funding_rate, bool)
                    or not isinstance(mark.last_funding_rate, (int, float))
                    or not math.isfinite(mark.last_funding_rate)):
                reasons.append("funding rate is invalid")
            next_funding_at = mark.next_funding_at
            if (not isinstance(next_funding_at, datetime)
                    or next_funding_at.tzinfo is None
                    or next_funding_at.utcoffset() is None):
                reasons.append("next funding timestamp is not timezone-aware")
            elif next_funding_at.astimezone(timezone.utc) <= validation_now:
                reasons.append("next funding timestamp is not in the future")

        if mark.observed_at.tzinfo is None or mark.observed_at.utcoffset() is None:
            reasons.append("mark-price timestamp is not timezone-aware")
        else:
            mark_age = (
                validation_now - mark.observed_at.astimezone(timezone.utc)
            ).total_seconds()
            if mark_age < -1.0:
                reasons.append("mark-price timestamp is in the future")
            elif mark_age > self.config.max_market_age_seconds:
                reasons.append("mark-price data is stale")

        closed = tuple(c for c in candles if c.is_closed)
        if not closed:
            reasons.append("no fully closed candles are available")
        else:
            if any(c.symbol != symbol for c in closed):
                reasons.append("candle symbol does not match requested symbol")
            if any(c.close_time > validation_now for c in closed):
                reasons.append("closed candle has a future close timestamp")
            if any(c.close_time <= c.open_time for c in closed):
                reasons.append("candle has invalid open/close timestamps")
            if any(closed[i].open_time <= closed[i - 1].open_time for i in range(1, len(closed))):
                reasons.append("candle timestamps are not strictly increasing")
            else:
                interval = timedelta(seconds=INTERVAL_SECONDS[self.config.interval])
                for previous, current in zip(closed, closed[1:]):
                    if current.open_time - previous.open_time != interval:
                        reasons.append("candle gap detected between consecutive closed candles")
                        break
            candle_age = (validation_now - closed[-1].close_time).total_seconds()
            if candle_age < 0:
                reasons.append("latest closed candle is future-dated")
            elif candle_age > self.config.max_market_age_seconds:
                reasons.append("latest closed candle is stale")

        if reasons:
            if any("gap" in reason for reason in reasons):
                status = FeedStatus.GAP_DETECTED
            elif any("stale" in reason or "future" in reason for reason in reasons):
                status = FeedStatus.STALE
            elif any("no fully closed" in reason for reason in reasons):
                status = FeedStatus.NO_CLOSED_CANDLES
            else:
                status = FeedStatus.INVALID_DATA
            self._retry_counts[symbol] = 0
            return self._frame(symbol, status, now, None, mark.observed_at, (), tuple(reasons))

        self._retry_counts[symbol] = 0
        return self._frame(
            symbol, FeedStatus.HEALTHY, now, mark.mark_price, mark.observed_at,
            closed, (), funding_rate=mark.last_funding_rate,
            index_price=mark.index_price, next_funding_at=mark.next_funding_at,
        )

    def _frame(self, symbol, status, now, mark_price, mark_observed_at, candles, reasons, *,
               funding_rate=None, index_price=None, next_funding_at=None):
        return MarketFrame(
            symbol=symbol, status=status, checked_at=now, mark_price=mark_price,
            mark_observed_at=mark_observed_at, closed_candles=candles,
            reasons=tuple(reasons), retry_count=self._retry_counts[symbol], orders_authorized=False,
            funding_rate=funding_rate, index_price=index_price, next_funding_at=next_funding_at,
        )

    def run_forever(
        self,
        stop_event: threading.Event,
        *,
        on_result: Callable[[MarketPollResult], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Continuously poll until stopped; wait duration applies rate limiting/backoff."""
        injected_clock = clock is not None
        clock = clock or (lambda: datetime.now(timezone.utc))
        while not stop_event.is_set():
            try:
                # Keep deterministic clocks available for tests, but in production
                # let poll_once capture its own clock and validate after network I/O.
                result = self.poll_once(now=clock() if injected_clock else None)
            except Exception:
                # A malformed adapter response or clock error must not silently
                # terminate the long-running worker. No data is emitted for this
                # cycle; increase the bounded backoff and retry on the next loop.
                self._consecutive_failed_cycles += 1
                logger.exception("Market polling cycle failed; no market data was forwarded")
                stop_event.wait(self.next_delay_seconds)
                continue
            if on_result is not None:
                try:
                    on_result(result)
                except Exception:
                    # Consumer failures are operational errors, not market signals.
                    # Log them and keep polling; callers must monitor these logs.
                    logger.exception("Market poll consumer failed; worker will continue")
            stop_event.wait(self.next_delay_seconds)
