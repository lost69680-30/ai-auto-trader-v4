"""Scheduled, read-only multi-timeframe market intelligence scan."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import math
from typing import Any, Iterable, Mapping

from app.engines.price_planner import PricePlanRequest, PricePlanningConfig, plan_prices
from backend.adaptive_strategy import select_strategy
from backend.strategy_assessment_engine import MultiStrategyAssessmentEngine, StrategyCandidate
from backend.market_assessment import TIMEFRAME_ORDER, assess_market
from backend.indicator_intelligence import assess_indicators
from backend.market_data_adapter import MarketCandle, market_data_error_context
from backend.timeframe_aggregation import aggregate_complete_yearly_candles


logger = logging.getLogger(__name__)
SCAN_INTERVALS = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M")
DEFAULT_CANDLE_LIMIT = 200
OPPORTUNITY_TIMEFRAME_MAX_AGE_SECONDS = {
    "1m": 180, "5m": 600, "15m": 1800, "30m": 3600,
    "1h": 7200, "4h": 28800, "1d": 172800,
    "1w": 1209600, "1M": 2678400,
}


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
    strategy_engine: MultiStrategyAssessmentEngine | None = None,
    opportunity_coordinator: Any | None = None,
    plan_lifetime_seconds: float | None = None,
):
    """Create a durable-scheduler handler for market assessment and strategy ranking.

    The scan reads public market data only. When a venue-scoped coordinator is
    supplied, independently eligible assessments receive a fresh ATR price plan
    and are registered durably. This stage never creates a paper intent, risk
    approval, fill, position, or exchange order.
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

    if opportunity_coordinator is not None and (
        getattr(opportunity_coordinator, "market_type", None) != market_type
        or getattr(opportunity_coordinator, "venue", None) != venue
        or not callable(getattr(opportunity_coordinator, "register_assessed_opportunity", None))
    ):
        raise ValueError("opportunity coordinator must match this scan's isolated Testnet venue")
    if plan_lifetime_seconds is not None and (
        isinstance(plan_lifetime_seconds, bool)
        or not isinstance(plan_lifetime_seconds, (int, float))
        or not math.isfinite(plan_lifetime_seconds)
        or not 30 <= plan_lifetime_seconds <= 86400
    ):
        raise ValueError("plan_lifetime_seconds must be between 30 and 86400 seconds")

    engine = strategy_engine or MultiStrategyAssessmentEngine()

    def handle(payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        if payload.get("scope") != "market_intelligence":
            raise ValueError("market intelligence scan requires scope=market_intelligence")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("market intelligence idempotency key is required")

        now = datetime.now(timezone.utc)
        universe = source.fetch_market_universe(now=now)
        snapshot_counts: dict[str, int] = {}
        snapshot_by_symbol: dict[str, Any] = {}
        contract_counts: dict[str, int] = {}
        contract_by_symbol: dict[str, Any] = {}
        for contract in getattr(universe, "contracts", ()):
            contract_counts[contract.symbol] = contract_counts.get(contract.symbol, 0) + 1
            contract_by_symbol[contract.symbol] = contract
        for snapshot in universe.snapshots:
            snapshot_counts[snapshot.symbol] = snapshot_counts.get(snapshot.symbol, 0) + 1
            snapshot_by_symbol[snapshot.symbol] = snapshot

        fetched: dict[tuple[str, str], tuple[MarketCandle, ...]] = {}
        in_progress_by_symbol: dict[str, list[str]] = {symbol: [] for symbol in symbols}
        errors_by_symbol: dict[str, dict[str, str]] = {symbol: {} for symbol in symbols}

        def fetch_one(symbol: str, interval: str) -> tuple[str, str, tuple[MarketCandle, ...]]:
            rows = tuple(source.fetch_klines(
                symbol, interval=interval, limit=candle_limit,
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
                        "market intelligence timeframe unavailable symbol=%s interval=%s error=%s detail=%s",
                        symbol, interval, type(exc).__name__, market_data_error_context(exc),
                    )

        # Kline requests finish after the scan starts. Use the latest closed
        # candle boundary as the analysis clock, not the stale scan-start clock
        # that was supplied before the concurrent requests began.
        latest_closed_times = [rows[-1].close_time for rows in fetched.values() if rows]
        if latest_closed_times:
            now = max(now, max(latest_closed_times))

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
            indicator_assessments = {
                interval: assess_indicators(symbol, interval, rows, now=now)
                for interval, rows in candle_map.items()
            }
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
            evidence_refs = []
            if snapshot_fresh and snapshot is not None:
                evidence_refs.append(
                    f"market-snapshot:{symbol}:{snapshot.observed_at.astimezone(timezone.utc).isoformat()}"
                )
            for interval, rows in candle_map.items():
                if rows:
                    evidence_refs.append(
                        f"closed-candle:{symbol}:{interval}:{rows[-1].close_time.astimezone(timezone.utc).isoformat()}"
                    )
            candidate_id = hashlib.sha256(
                f"{idempotency_key}|{symbol}".encode("utf-8")
            ).hexdigest()[:40]
            independent_assessments = engine.assess(
                StrategyCandidate(
                    opportunity_id=candidate_id,
                    symbol=symbol,
                    market_type=market_type,
                    market_assessment=assessment,
                    spread_bps=spread,
                    quote_volume_24h=volume,
                    estimated_round_trip_cost_bps=cost_bps,
                    assessment_version=max(1, int(now.timestamp() * 1000)),
                    orders_authorized=False,
                    indicator_assessments=indicator_assessments,
                ),
                evidence_refs,
                now=now,
            )
            opportunity_registrations: list[dict[str, Any]] = []
            if opportunity_coordinator is not None:
                contract = contract_by_symbol.get(symbol) if contract_counts.get(symbol) == 1 else None
                for strategy_assessment in independent_assessments:
                    if (
                        strategy_assessment.decision != "ELIGIBLE"
                        or strategy_assessment.payload.get("eligible_for_risk_review") is not True
                        or strategy_assessment.payload.get("paper_eligible") is not True
                        or strategy_assessment.payload.get("analysis_only") is True
                    ):
                        continue
                    required = strategy_assessment.payload.get("required_timeframes")
                    timeframe = required[0] if isinstance(required, list) and required else None
                    timeframe_age = OPPORTUNITY_TIMEFRAME_MAX_AGE_SECONDS.get(timeframe)
                    if (
                        contract is None
                        or getattr(contract, "status", None) != "TRADING"
                        or not isinstance(getattr(contract, "tick_size", None), (int, float))
                        or isinstance(getattr(contract, "tick_size", None), bool)
                        or not math.isfinite(float(contract.tick_size))
                        or float(contract.tick_size) <= 0
                        or timeframe_age is None
                        or not candle_map.get(timeframe)
                        or not snapshot_fresh
                        or cost_pct is None
                    ):
                        opportunity_registrations.append({
                            "strategy_id": strategy_assessment.strategy_id,
                            "status": "NO_PLAN",
                            "reason": "required contract, liquidity, cost or closed-candle evidence is unavailable",
                            "orders_authorized": False,
                        })
                        continue
                    try:
                        price_snapshot = source.fetch_mark_price(symbol)
                        reference_price = getattr(price_snapshot, "mark_price", None)
                        reference_at = getattr(price_snapshot, "observed_at", None)
                        reference_age = (
                            (now - reference_at.astimezone(timezone.utc)).total_seconds()
                            if isinstance(reference_at, datetime)
                            and reference_at.tzinfo is not None
                            and reference_at.utcoffset() is not None
                            else math.inf
                        )
                        if (
                            getattr(price_snapshot, "symbol", None) != symbol
                            or getattr(price_snapshot, "market_type", None) != market_type
                            or getattr(price_snapshot, "orders_authorized", None) is not False
                            or not isinstance(reference_price, (int, float))
                            or isinstance(reference_price, bool)
                            or not math.isfinite(reference_price)
                            or reference_price <= 0
                            or reference_age < -2
                            or reference_age > 30
                        ):
                            raise ValueError("reference price failed venue or freshness validation")
                        latest_candle = candle_map[timeframe][-1]
                        signal_material = "|".join((
                            market_type, venue, symbol, strategy_assessment.strategy_id,
                            str(strategy_assessment.payload["direction"]), timeframe,
                            latest_candle.close_time.astimezone(timezone.utc).isoformat(),
                            format(float(reference_price), ".12g"),
                            now.astimezone(timezone.utc).isoformat(),
                        ))
                        signal_id = "signal-" + hashlib.sha256(
                            signal_material.encode("utf-8")
                        ).hexdigest()[:40]
                        lifetime = (
                            float(plan_lifetime_seconds)
                            if plan_lifetime_seconds is not None
                            else float(min(86400, max(900, timeframe_age)))
                        )
                        plan_result = plan_prices(
                            PricePlanRequest(
                                signal_id=signal_id,
                                symbol=symbol,
                                direction=strategy_assessment.payload["direction"],
                                reference_price=float(reference_price),
                                reference_price_at=reference_at,
                                candles=tuple(candle_map[timeframe]),
                                now=now,
                                expires_at=now + timedelta(seconds=lifetime),
                                estimated_round_trip_cost_pct=float(cost_pct),
                                tick_size=float(contract.tick_size),
                            ),
                            PricePlanningConfig(max_candle_age_seconds=timeframe_age),
                        )
                        if plan_result.status != "READY":
                            opportunity_registrations.append({
                                "strategy_id": strategy_assessment.strategy_id,
                                "status": "NO_PLAN",
                                "reason": plan_result.reason,
                                "orders_authorized": False,
                            })
                            continue
                        saved_opportunity = opportunity_coordinator.register_assessed_opportunity(
                            strategy_assessment,
                            price_plan_result=plan_result,
                            symbol=symbol,
                            market_type=market_type,
                            venue=venue,
                            timeframe=timeframe,
                            evidence_refs=evidence_refs,
                            now=now,
                        )
                        opportunity_registrations.append({
                            "strategy_id": strategy_assessment.strategy_id,
                            "status": "REGISTERED",
                            "opportunity_id": saved_opportunity.opportunity_id,
                            "state": saved_opportunity.state,
                            "orders_authorized": False,
                        })
                    except Exception as exc:
                        logger.warning(
                            "opportunity registration failed closed symbol=%s strategy=%s error=%s",
                            symbol, strategy_assessment.strategy_id, type(exc).__name__,
                        )
                        opportunity_registrations.append({
                            "strategy_id": strategy_assessment.strategy_id,
                            "status": "REGISTRATION_FAILED",
                            "reason": type(exc).__name__,
                            "orders_authorized": False,
                        })

            reports.append({
                "symbol": symbol,
                "opportunity_registrations": opportunity_registrations,
                "assessment": assessment,
                "indicator_assessments": indicator_assessments,
                "selection": selection,
                "strategy_assessments": independent_assessments,
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

        ranked = sorted(
            (
                report for report in reports
                if report["selection"].eligible_for_risk_review
                and report["selection"].fit_score is not None
            ),
            key=lambda report: (
                -float(report["selection"].fit_score),
                report["symbol"],
            ),
        )
        rank_by_symbol = {report["symbol"]: rank for rank, report in enumerate(ranked, start=1)}
        for report in reports:
            selection_payload = _json_safe(report["selection"])
            selection_payload["opportunity_rank"] = rank_by_symbol.get(report["symbol"])
            selection_payload["strategy_assessments"] = _json_safe(report["strategy_assessments"])
            report["selection"] = selection_payload

        result_payload = _json_safe({
            "scan_id": idempotency_key,
            "assessed_at": now,
            "market_type": market_type,
            "environment": environment,
            "venue": venue,
            "symbols": reports,
            "orders_authorized": False,
        })
        store.publish_intelligence(result_payload, now=now)
        logger.info(
            "market intelligence scan complete symbols=%d strategy_assessments=%d orders_authorized=False",
            len(reports), sum(len(report["strategy_assessments"]) for report in reports),
        )
        return result_payload

    return handle
