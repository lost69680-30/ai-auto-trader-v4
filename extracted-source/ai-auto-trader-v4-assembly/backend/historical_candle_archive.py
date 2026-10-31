"""Durable, read-only canonical one-minute candle archive.

This module is deliberately separate from the rolling live-feed snapshot store.
It contains no exchange client, credentials, order APIs, or risk authorization.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

try:
    import psycopg
except ImportError:  # pragma: no cover - deployment dependency
    psycopg = None

from app.engines.postgres_ssl import postgres_connection_ssl_options


@dataclass(frozen=True, slots=True)
class ArchivedCandle:
    venue: str
    market_type: str
    symbol: str
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool = True
    interval: str = "1m"


def _aware_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _finite(value: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{field} must be a finite number")
    return float(value)


def validate_minute_candle(candle: ArchivedCandle, *, now: datetime) -> ArchivedCandle:
    """Validate a closed, UTC-aligned one-minute OHLCV candle."""
    if not isinstance(candle, ArchivedCandle):
        raise ValueError("candle must be an ArchivedCandle")
    if candle.interval != "1m":
        raise ValueError("canonical archive accepts only one-minute candles")
    if candle.venue not in {"BINANCE_USDM_TESTNET", "BINANCE_SPOT_TESTNET"}:
        raise ValueError("unsupported or ambiguous market-data venue")
    if candle.market_type not in {"FUTURES", "SPOT"}:
        raise ValueError("market_type must be FUTURES or SPOT")
    if (candle.market_type == "FUTURES" and candle.venue != "BINANCE_USDM_TESTNET"
            or candle.market_type == "SPOT" and candle.venue != "BINANCE_SPOT_TESTNET"):
        raise ValueError("venue and market_type do not match")
    if not isinstance(candle.symbol, str) or not candle.symbol or candle.symbol != candle.symbol.upper().strip():
        raise ValueError("symbol must be non-empty, trimmed and uppercase-normalized")
    opened = _aware_utc(candle.open_time, "open_time")
    closed = _aware_utc(candle.close_time, "close_time")
    current = _aware_utc(now, "now")
    if opened.second != 0 or opened.microsecond != 0:
        raise ValueError("one-minute candle open_time must align to a UTC minute")
    if closed <= opened or closed >= opened + timedelta(minutes=1):
        raise ValueError("one-minute candle close_time is outside its minute")
    if opened >= current or closed >= current:
        raise ValueError("future or not-yet-closed candle cannot be archived")
    if candle.is_closed is not True:
        raise ValueError("only fully closed candles can be archived")
    opened_price = _finite(candle.open, "open")
    high = _finite(candle.high, "high")
    low = _finite(candle.low, "low")
    close = _finite(candle.close, "close")
    volume = _finite(candle.volume, "volume")
    if min(opened_price, high, low, close) <= 0 or volume < 0:
        raise ValueError("OHLC prices must be positive and volume non-negative")
    if high < max(opened_price, close, low) or low > min(opened_price, close, high):
        raise ValueError("OHLC geometry is invalid")
    return ArchivedCandle(
        venue=candle.venue, market_type=candle.market_type, symbol=candle.symbol,
        open_time=opened, close_time=closed, open=opened_price, high=high,
        low=low, close=close, volume=volume, is_closed=True, interval="1m",
    )


def aggregate_candles(
    candles: Iterable[ArchivedCandle], *, interval: str,
    now: datetime | None = None,
) -> tuple[ArchivedCandle, ...]:
    """Derive fixed-duration bars only from complete, contiguous minute windows."""
    seconds_by_interval = {
        "5m": 300, "15m": 900, "30m": 1800, "1h": 3600,
        "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800,
        "12h": 43200, "1d": 86400,
    }
    seconds = seconds_by_interval.get(interval)
    if seconds is None:
        raise ValueError("unsupported fixed-duration derived interval")
    raw_rows = tuple(candles)
    if not raw_rows:
        return ()
    current = now or datetime.now(timezone.utc)
    rows = tuple(validate_minute_candle(row, now=current) for row in raw_rows)
    identity = (rows[0].venue, rows[0].market_type, rows[0].symbol)
    if any((row.venue, row.market_type, row.symbol) != identity for row in rows):
        raise ValueError("cannot aggregate across venues, market types, or symbols")
    ordered = tuple(sorted(rows, key=lambda row: row.open_time))
    if len({row.open_time for row in ordered}) != len(ordered):
        raise ValueError("duplicate one-minute timestamps cannot be aggregated")
    result: list[ArchivedCandle] = []
    cursor = 0
    while cursor < len(ordered):
        first = ordered[cursor]
        start_epoch = int(first.open_time.timestamp())
        window_start_epoch = start_epoch - (start_epoch % seconds)
        window_start = datetime.fromtimestamp(window_start_epoch, timezone.utc)
        expected_count = seconds // 60
        window = ordered[cursor:cursor + expected_count]
        if len(window) != expected_count:
            raise ValueError(f"incomplete {interval} window")
        if first.open_time != window_start:
            raise ValueError(f"{interval} window starts with a missing minute")
        for offset, row in enumerate(window):
            expected = window_start + timedelta(minutes=offset)
            if row.open_time != expected:
                raise ValueError(f"incomplete or gapped {interval} window")
        result.append(ArchivedCandle(
            venue=identity[0], market_type=identity[1], symbol=identity[2],
            open_time=window_start, close_time=window[-1].close_time,
            open=window[0].open, high=max(row.high for row in window),
            low=min(row.low for row in window), close=window[-1].close,
            volume=sum(row.volume for row in window), is_closed=True, interval=interval,
        ))
        cursor += expected_count
    return tuple(result)


class PostgresCandleArchive:
    """Insert and query canonical closed candles in the private paper_ledger schema."""

    def __init__(self, dsn: str) -> None:
        if psycopg is None:
            raise RuntimeError("Historical candle archive requires psycopg.")
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("PAPER_DATABASE_URL must be configured for candle archive.")
        self._dsn = dsn.strip()

    def _connect(self):
        return psycopg.connect(
            self._dsn, connect_timeout=10, **postgres_connection_ssl_options(self._dsn)
        )

    def insert_closed_candles(
        self, candles: Iterable[ArchivedCandle], *, now: datetime | None = None
    ) -> int:
        current = now or datetime.now(timezone.utc)
        validated = tuple(validate_minute_candle(row, now=current) for row in candles)
        if not validated:
            return 0
        inserted = 0
        with self._connect() as conn:
            for row in validated:
                result = conn.execute(
                    """INSERT INTO paper_ledger.market_candles
                       (venue, market_type, symbol, interval, open_time, close_time,
                        open, high, low, close, volume, orders_authorized)
                       VALUES (%s, %s, %s, '1m', %s, %s, %s, %s, %s, %s, %s, false)
                       ON CONFLICT (venue, market_type, symbol, interval, open_time)
                       DO NOTHING RETURNING open_time""",
                    (row.venue, row.market_type, row.symbol, row.open_time, row.close_time,
                     row.open, row.high, row.low, row.close, row.volume),
                )
                if result.fetchone() is not None:
                    inserted += 1
        return inserted

    def read_closed_candles(
        self, *, venue: str, market_type: str, symbol: str,
        start: datetime, end: datetime, limit: int = 10000,
    ) -> tuple[ArchivedCandle, ...]:
        start_utc = _aware_utc(start, "start")
        end_utc = _aware_utc(end, "end")
        if end_utc <= start_utc:
            raise ValueError("end must be later than start")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100000:
            raise ValueError("limit must be from 1 through 100000")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT venue, market_type, symbol, open_time, close_time,
                          open, high, low, close, volume
                   FROM paper_ledger.market_candles
                   WHERE venue = %s AND market_type = %s AND symbol = %s
                     AND interval = '1m' AND open_time >= %s AND open_time < %s
                     AND orders_authorized = false
                   ORDER BY open_time ASC LIMIT %s""",
                (venue, market_type, symbol, start_utc, end_utc, limit),
            ).fetchall()
        return tuple(ArchivedCandle(
            venue=row[0], market_type=row[1], symbol=row[2], open_time=row[3],
            close_time=row[4], open=float(row[5]), high=float(row[6]),
            low=float(row[7]), close=float(row[8]), volume=float(row[9]),
            is_closed=True, interval="1m",
        ) for row in rows)
