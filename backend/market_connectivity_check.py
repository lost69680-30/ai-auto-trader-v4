"""Read-only Binance USDⓈ-M Futures connectivity diagnostics.

The checker uses only public GET market-data endpoints: server time, premium index,
and klines. It never reads credentials or sends orders. Testnet is selected by the
adapter by default in the CLI unless production public data is explicitly chosen.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
import time
from typing import Callable

from backend.market_data_adapter import BinanceUsdMFuturesAdapter, MarketDataError


@dataclass(frozen=True, slots=True)
class EndpointCheck:
    name: str
    ok: bool
    elapsed_ms: float
    detail: str


@dataclass(frozen=True, slots=True)
class ConnectivityReport:
    base_url: str
    symbol: str
    checked_at: str
    endpoints: tuple[EndpointCheck, ...]
    server_time: str | None
    clock_skew_ms: float | None
    clock_skew_ok: bool
    mark_price: float | None
    closed_candle_count: int
    error: str | None = None
    orders_authorized: bool = False

    @property
    def passed(self) -> bool:
        return not self.error and self.clock_skew_ok and bool(self.endpoints) and all(
            item.ok for item in self.endpoints
        ) and self.orders_authorized is False

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["passed"] = self.passed
        return result


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def run_public_connectivity_check(
    adapter: BinanceUsdMFuturesAdapter,
    symbol: str = "BTCUSDT",
    *,
    clock: Callable[[], datetime] | None = None,
    max_clock_skew_ms: float = 5_000.0,
    max_mark_age_seconds: float = 15.0,
) -> ConnectivityReport:
    """Probe public REST connectivity and data freshness without order access.

    ``clock`` is injectable for deterministic tests. The exchange time is compared
    with the midpoint of the local request's before/after timestamps to reduce
    apparent skew caused by request latency.
    """
    clock = clock or (lambda: datetime.now(timezone.utc))
    checked = clock()
    if not _aware(checked):
        raise ValueError("connectivity-check clock must return a timezone-aware datetime")
    checked = checked.astimezone(timezone.utc)
    if not isinstance(symbol, str) or not symbol or symbol != symbol.strip().upper():
        return ConnectivityReport(
            base_url=adapter.base_url, symbol=str(symbol), checked_at=checked.isoformat(),
            endpoints=(), server_time=None, clock_skew_ms=None, clock_skew_ok=False,
            mark_price=None, closed_candle_count=0,
            error="Symbol must be non-empty, trimmed, and uppercase-normalized.",
        )
    for name, value in (("max_clock_skew_ms", max_clock_skew_ms),
                        ("max_mark_age_seconds", max_mark_age_seconds)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")

    checks: list[EndpointCheck] = []
    time_before = clock()
    if not _aware(time_before):
        raise ValueError("connectivity-check clock must return timezone-aware datetimes")
    start = time.perf_counter()
    try:
        server_time = adapter.fetch_server_time()
        elapsed = (time.perf_counter() - start) * 1000.0
        checks.append(EndpointCheck("server_time", True, round(elapsed, 3), "Public server-time endpoint returned a valid timestamp."))
    except Exception as exc:
        elapsed = (time.perf_counter() - start) * 1000.0
        checks.append(EndpointCheck("server_time", False, round(elapsed, 3), f"{type(exc).__name__}: {exc}"))
        return ConnectivityReport(
            base_url=adapter.base_url, symbol=symbol, checked_at=checked.isoformat(),
            endpoints=tuple(checks), server_time=None, clock_skew_ms=None,
            clock_skew_ok=False, mark_price=None, closed_candle_count=0,
            error="Cannot reach or validate exchange server time; remaining probes skipped.",
        )

    time_after = clock()
    if not _aware(time_after):
        raise ValueError("connectivity-check clock must return timezone-aware datetimes")
    midpoint = (time_before.timestamp() + time_after.timestamp()) / 2.0
    skew_ms = (server_time.timestamp() - midpoint) * 1000.0
    skew_ok = math.isfinite(skew_ms) and abs(skew_ms) <= max_clock_skew_ms

    mark_price: float | None = None
    start = time.perf_counter()
    try:
        mark = adapter.fetch_mark_price(symbol)
        mark_price = mark.mark_price
        mark_age = (server_time - mark.observed_at.astimezone(timezone.utc)).total_seconds()
        if mark.symbol != symbol or mark.orders_authorized is not False:
            raise MarketDataError("mark response symbol or authorization boundary is invalid")
        if not math.isfinite(mark.mark_price) or mark.mark_price <= 0:
            raise MarketDataError("mark price is not finite and positive")
        if not 0 <= mark_age <= max_mark_age_seconds:
            raise MarketDataError(f"mark observation is stale or future-dated ({mark_age:.3f}s relative to server time)")
        checks.append(EndpointCheck(
            "mark_price", True, round((time.perf_counter() - start) * 1000.0, 3),
            f"Received fresh public mark price; age={mark_age:.3f}s.",
        ))
    except Exception as exc:
        checks.append(EndpointCheck(
            "mark_price", False, round((time.perf_counter() - start) * 1000.0, 3),
            f"{type(exc).__name__}: {exc}",
        ))

    closed_count = 0
    start = time.perf_counter()
    try:
        candles = adapter.fetch_klines(symbol, interval="1m", limit=3, now=server_time)
        closed = [candle for candle in candles if candle.is_closed is True]
        if not closed:
            raise MarketDataError("no fully closed 1-minute candle was returned")
        latest_age = (server_time - closed[-1].close_time).total_seconds()
        if latest_age < 0 or latest_age > 120:
            raise MarketDataError(f"latest closed candle is stale or future-dated ({latest_age:.3f}s)")
        closed_count = len(closed)
        checks.append(EndpointCheck(
            "closed_klines", True, round((time.perf_counter() - start) * 1000.0, 3),
            f"Received {closed_count} closed 1-minute candle(s); latest close age={latest_age:.3f}s.",
        ))
    except Exception as exc:
        checks.append(EndpointCheck(
            "closed_klines", False, round((time.perf_counter() - start) * 1000.0, 3),
            f"{type(exc).__name__}: {exc}",
        ))

    return ConnectivityReport(
        base_url=adapter.base_url, symbol=symbol, checked_at=checked.isoformat(),
        endpoints=tuple(checks), server_time=server_time.isoformat(),
        clock_skew_ms=round(skew_ms, 3), clock_skew_ok=skew_ok,
        mark_price=mark_price, closed_candle_count=closed_count,
        error=None if skew_ok else f"Local clock differs from exchange time by {skew_ms:.1f}ms; limit is {max_clock_skew_ms:.1f}ms.",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTCUSDT", help="uppercase USDⓈ-M Futures symbol")
    parser.add_argument("--timeout", type=float, default=5.0, help="per-request timeout in seconds")
    parser.add_argument("--max-clock-skew-ms", type=float, default=5000.0)
    parser.add_argument("--production-public-data", action="store_true",
                        help="probe production public data instead of Testnet; still GET-only and paper-only")
    args = parser.parse_args(argv)
    if args.timeout <= 0 or not math.isfinite(args.timeout):
        parser.error("--timeout must be finite and positive")
    if args.max_clock_skew_ms <= 0 or not math.isfinite(args.max_clock_skew_ms):
        parser.error("--max-clock-skew-ms must be finite and positive")
    adapter = BinanceUsdMFuturesAdapter(testnet=not args.production_public_data, timeout_seconds=args.timeout)
    report = run_public_connectivity_check(adapter, args.symbol, max_clock_skew_ms=args.max_clock_skew_ms)
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 0 if report.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
