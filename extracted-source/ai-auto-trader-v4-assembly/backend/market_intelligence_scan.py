"""Scheduled, read-only multi-timeframe market intelligence scan."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import logging
import math
from typing import Any, Iterable, Mapping

from backend.adaptive_strategy import select_strategy
from backend.market_assessment import TIMEFRAME_ORDER, assess_market
from backend.market_data_adapter import MarketCandle
from backend.timeframe_aggregation import aggregate_complete_yearly_candles


logger = logging.getLogger(__name__)
SCAN_INTERVALS = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M")
DEFAULT_CANDLE_LIMIT = 200


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("market intelligence timestamps must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat()
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("market intelligence cannot contain non-finite values")
    return value


def _yearly_candles(symbol: str, monthly: Iterable[MarketCandle]) -> tuple[MarketCandle, ...]:
    result = []
    for row in aggregate_complete_yearly_candles(monthly):
        result.append(MarketCandle(
            symbol=symbol,
            open_time=datetime.fromisoformat(str(row["open_time"])),
            close_time=datetime.fromisoformat(str(row["close_time"])),
            open=float(row["open"]), high=float(row["high"]),
            low=float(row["low"]), close=float(row["close"]),
            volume=float(row["volume"]), quote_volume=0.0, trades=0,
            is_closed=True,
        ))
    return tuple(result)


def market_intelligence_scan(
    store: Any,
    source: Any,
    symbol_policies: Iterable[Any],
    *,
    market_type: str,
    environment: str = "TESTNET",
    venue: str = "BINANCE_USDM_TESTNET",
    candle_limit: int = DEFAULT_CANDLE_LIMIT,
    max_snapshot_age_seconds: float = 120.0,
    max_fetch_workers: int = 12,
):
    """Create a durable-scheduler handler for market assessment and strategy ranking.

    The scan reads public market data only. It never creates a signal, paper intent,
    order, fill, position, or risk-policy change. Its only write is an advisory
    assessment payload through the supplied shared-market store.
    """
    policies = tuple(symbol_policies)
    if not policies or len(policies) > 20:
        raise ValueError("market intelligence requires 1 to 20 configured symbol policies")
    if market_type not in {"SPOT", "FUTURES"}:
        raise ValueError("market_type must be SPOT or FUTURES")
    if environment != "TESTNET":
        raise ValueError("this shared worker scan is restricted to Testnet market data")
    expected_venue = "BINANCE_USDM_TESTNET" if market_type == "FUTURES" else "BINANCE_SPOT_TESTNET"
    if venue != expected_venue:
        raise ValueError("venue and market_type do not match the Testnet adapter")
    if isinstance(candle_limit, bool) or not isinstance(candle_limit, int) or not 30 <= candle_limit <= 500:
        raise ValueError("candle_limit must be between 30 and 500")
    if not math.isfinite(max_snapshot_age_seconds) or max_snapshot_age_seconds <= 0:
        raise ValueError("max_snapshot_age_seconds must be finite and positive")
    if isinstance(max_fetch_workers, bool) or not isinstance(max_fetch_workers, int) or not 1 <= max_fetch_workers <= 20:
        raise ValueError("max_fetch_workers must be between 1 and 20")
    symbols = [getattr(policy, "symbol", None) for policy in policies]
    if any(not isinstance(symbol, str) or not symbol or symbol != symbol.strip().upper() for symbol in symbols):
        raise ValueError("each symbol policy must expose a trimmed uppercase symbol")
    if len(set(symbols)) != len(symbols):
        raise ValueError("market intelligence symbol policies must be unique")

    def handle(payload: dict[str, Any], idempotency_key: str) -> None:
        if payload.get("scope") != "market_intelligence":
            raise ValueError("market intelligence scan requires scope=market_intelligence")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("market intelligence idempotency key is required")

        now = datetime.now(timezone.utc)
        universe = source.fetch_market_universe(now=now)
        snapshot_counts: dict[str, int] = {}
        snapshot_by_symbol: dict[str, Any] = {}
        for snapshot in universe.snapshots:
            snapshot_counts[snapshot.symbol] = snapshot_counts.get(snapshot.symbol, 0) + 1
            snapshot_by_symbol[snapshot.symbol] = snapshot

        fetched: dict[tuple[str, str], tuple[MarketCandle, ...]] = {}
        in_progress_by_symbol: dict[str, list[str]] = {symbol: [] for symbol in symbols}
        errors_by_symbol: dict[str, dict[str, str]] = {symbol: {} for symbol in symbols}

        def fetch_one(symbol: str, interval: str) -> tuple[str, str, tuple[MarketCandle, ...]]:
            rows = tuple(source.fetch_klines(
                symbol, interval=interval, limit=candle_limit, now=now,
            ))
            if any(not isinstance(row, MarketCandle) for row in rows):
                raise ValueError("market source returned a malformed candle row")
            return symbol, interval, rows

        with ThreadPoolExecutor(max_workers=min(max_fetch_workers, len(symbols) * len(SCAN_INTERVALS))) as pool:
            futures = {
                pool.submit(fetch_one, symbol, interval): (symbol, interval)
                for symbol in symbols for interval in SCAN_INTERVALS
            }
            for future in as_completed(futures):
                symbol, interval = futures[future]
                try:
                    _symbol, _interval, rows = future.result()
                    if any(not row.is_closed for row in rows):
                        in_progress_by_symbol[symbol].append(interval)
                    fetched[(symbol, interval)] = tuple(row for row in rows if row.is_closed)
                except Exception as exc:
                    errors_by_symbol[symbol][interval] = type(exc).__name__
                    fetched[(symbol, interval)] = ()
                    logger.warning(
                        "market intelligence timeframe unavailable symbol=%s interval=%s error=%s",
                        symbol, interval, type(exc).__name__,
                    )

        reports: list[dict[str, Any]] = []
        for policy in policies:
            symbol = policy.symbol
            candle_map = {
                interval: fetched.get((symbol, interval), ())
                for interval in SCAN_INTERVALS
            }
            in_progress = sorted(in_progress_by_symbol[symbol])
            timeframe_errors = errors_by_symbol[symbol]
            candle_map["1Y"] = _yearly_candles(symbol, candle_map.get("1M", ()))

            assessment = assess_market(symbol, candle_map, now=now)
            snapshot = snapshot_by_symbol.get(symbol) if snapshot_counts.get(symbol) == 1 else None
            snapshot_fresh = False
            if snapshot is not None:
                observed_at = getattr(snapshot, "observed_at", None)
                if isinstance(observed_at, datetime) and observed_at.tzinfo is not None and observed_at.utcoffset() is not None:
                    age = (now - observed_at.astimezone(timezone.utc)).total_seconds()
                    snapshot_fresh = -1.0 <= age <= max_snapshot_age_seconds

            spread = getattr(snapshot, "spread_bps", None) if snapshot_fresh else None
            volume = getattr(snapshot, "quote_volume_24h", None) if snapshot_fresh else None
            cost_pct = getattr(policy, "estimated_round_trip_cost_pct", None)
            cost_bps = float(cost_pct) * 100.0 if (
                isinstance(cost_pct, (int, float)) and not isinstance(cost_pct, bool)
                and math.isfinite(cost_pct) and cost_pct >= 0
            ) else None

            selection = select_strategy(
                assessment,
                market_type=market_type,
                spread_bps=spread,
                quote_volume_24h=volume,
                estimated_round_trip_cost_bps=cost_bps,
            )
            reports.append({
                "symbol": symbol,
                "assessment": assessment,
                "selection": selection,
                "liquidity_evidence": {
                    "snapshot_fresh": snapshot_fresh,
                    "spread_bps": spread,
                    "quote_volume_24h": volume,
                    "estimated_round_trip_cost_bps": cost_bps,
                },
                "in_progress_timeframes": in_progress,
                "timeframe_errors": timeframe_errors,
                "orders_authorized": False,
            })

        store.publish_intelligence(_json_safe({
            "scan_id": idempotency_key,
            "assessed_at": now,
            "market_type": market_type,
            "environment": environment,
            "venue": venue,
            "symbols": reports,
            "orders_authorized": False,
        }), now=now)
        logger.info(
            "market intelligence scan complete symbols=%d orders_authorized=False",
            len(reports),
        )

    return handle
