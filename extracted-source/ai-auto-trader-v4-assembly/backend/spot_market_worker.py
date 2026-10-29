"""Read-only Spot market polling worker with no Futures-only price fields."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Protocol

from backend.market_data_adapter import MarketCandle, SpotPriceSnapshot, MarketDataError


INTERVAL_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800,
    "12h": 43200, "1d": 86400, "3d": 259200, "1w": 604800,
}


class SpotMarketDataSource(Protocol):
    def fetch_klines(self, symbol: str, *, interval: str, limit: int,
                     now: datetime | None = None) -> tuple[MarketCandle, ...]: ...
    def fetch_spot_price(self, symbol: str) -> SpotPriceSnapshot: ...


@dataclass(frozen=True, slots=True)
class SpotMarketWorkerConfig:
    symbols: tuple[str, ...]
    interval: str = "1m"
    candle_limit: int = 100
    max_market_age_seconds: float = 90.0

    def __post_init__(self) -> None:
        if not self.symbols or any(not isinstance(s, str) or not s or s != s.upper().strip()
                                   or not s.isalnum() for s in self.symbols):
            raise ValueError("symbols must be a non-empty tuple of uppercase alphanumeric symbols")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols must be unique")
        if self.interval not in INTERVAL_SECONDS:
            raise ValueError("unsupported interval for candle-gap monitoring")
        if isinstance(self.candle_limit, bool) or not isinstance(self.candle_limit, int) or not 2 <= self.candle_limit <= 1500:
            raise ValueError("candle_limit must be an integer from 2 through 1500")
        if not math.isfinite(self.max_market_age_seconds) or self.max_market_age_seconds <= 0:
            raise ValueError("max_market_age_seconds must be finite and positive")


@dataclass(frozen=True, slots=True)
class SpotMarketFrame:
    symbol: str
    status: str
    checked_at: datetime
    last_price: float | None
    last_observed_at: datetime | None
    closed_candles: tuple[MarketCandle, ...]
    reasons: tuple[str, ...]
    market_type: str = "SPOT"
    orders_authorized: bool = False


@dataclass(frozen=True, slots=True)
class SpotMarketPollResult:
    checked_at: datetime
    frames: tuple[SpotMarketFrame, ...]
    market_type: str = "SPOT"
    orders_authorized: bool = False

    @property
    def healthy_symbols(self) -> tuple[str, ...]:
        return tuple(frame.symbol for frame in self.frames if frame.status == "HEALTHY")

    @property
    def unhealthy_symbols(self) -> tuple[str, ...]:
        return tuple(frame.symbol for frame in self.frames if frame.status != "HEALTHY")


class SpotMarketDataWorker:
    """Validate public Spot last prices and closed candles; never asks for mark/funding data."""

    def __init__(self, source: SpotMarketDataSource, config: SpotMarketWorkerConfig) -> None:
        self.source = source
        self.config = config

    def poll_once(self, *, now: datetime | None = None) -> SpotMarketPollResult:
        explicit_now = now is not None
        checked_at = now or datetime.now(timezone.utc)
        if checked_at.tzinfo is None or checked_at.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        checked_at = checked_at.astimezone(timezone.utc)
        frames = tuple(self._poll_symbol(symbol, checked_at, explicit_now=explicit_now)
                       for symbol in self.config.symbols)
        return SpotMarketPollResult(checked_at, frames, orders_authorized=False)

    def _poll_symbol(self, symbol: str, now: datetime, *, explicit_now: bool) -> SpotMarketFrame:
        try:
            if explicit_now:
                candles = self.source.fetch_klines(
                    symbol, interval=self.config.interval, limit=self.config.candle_limit, now=now
                )
            else:
                candles = self.source.fetch_klines(
                    symbol, interval=self.config.interval, limit=self.config.candle_limit
                )
            price = self.source.fetch_spot_price(symbol)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}" if isinstance(exc, MarketDataError) else type(exc).__name__
            return self._frame(symbol, now, None, None, (), (f"Spot public data request failed: {detail}",), "DISCONNECTED")

        validation_now = now if explicit_now else datetime.now(timezone.utc)
        reasons: list[str] = []
        if price.symbol != symbol or not math.isfinite(price.last_price) or price.last_price <= 0:
            reasons.append("Spot last-price payload is invalid or symbol-mismatched")
        if price.observed_at.tzinfo is None or price.observed_at.utcoffset() is None:
            reasons.append("Spot last-price timestamp is not timezone-aware")
        else:
            age = (validation_now - price.observed_at.astimezone(timezone.utc)).total_seconds()
            if age < -1.0:
                reasons.append("Spot last-price timestamp is in the future")
            elif age > self.config.max_market_age_seconds:
                reasons.append("Spot last-price data is stale")

        closed = tuple(row for row in candles if row.is_closed)
        if not closed:
            reasons.append("no fully closed Spot candles are available")
        else:
            interval = timedelta(seconds=INTERVAL_SECONDS[self.config.interval])
            for index, row in enumerate(closed):
                if row.symbol != symbol:
                    reasons.append("Spot candle symbol mismatch")
                    break
                if row.open_time.tzinfo is None or row.open_time.utcoffset() is None or row.close_time.tzinfo is None or row.close_time.utcoffset() is None:
                    reasons.append("Spot candle timestamp is not timezone-aware")
                    break
                values = (row.open, row.high, row.low, row.close, row.volume)
                if (not all(math.isfinite(value) for value in values)
                        or min(row.open, row.high, row.low, row.close) <= 0
                        or row.volume < 0 or row.high < max(row.open, row.close, row.low)
                        or row.low > min(row.open, row.close, row.high)
                        or row.close_time <= row.open_time or row.close_time > validation_now):
                    reasons.append("Spot candle OHLCV or timestamp is invalid")
                    break
                if index and (row.open_time <= closed[index - 1].open_time
                              or row.open_time - closed[index - 1].open_time != interval):
                    reasons.append("Spot candle gap or ordering error detected")
                    break
            candle_age = (validation_now - closed[-1].close_time).total_seconds()
            if candle_age < 0:
                reasons.append("latest closed Spot candle is future-dated")
            elif candle_age > self.config.max_market_age_seconds:
                reasons.append("latest closed Spot candle is stale")

        if reasons:
            status = "STALE" if any("stale" in reason.lower() or "future" in reason.lower() for reason in reasons) else (
                "GAP_DETECTED" if any("gap" in reason.lower() for reason in reasons) else "INVALID_DATA"
            )
            return self._frame(symbol, now, None, price.observed_at, (), tuple(reasons), status)
        return self._frame(symbol, now, price.last_price, price.observed_at, closed, (), "HEALTHY")

    @staticmethod
    def _frame(symbol, now, price, observed, candles, reasons, status):
        return SpotMarketFrame(
            symbol=symbol, status=status, checked_at=now, last_price=price,
            last_observed_at=observed, closed_candles=tuple(candles),
            reasons=tuple(reasons), market_type="SPOT", orders_authorized=False,
        )
