"""Continuous Testnet opportunity search built on the existing shared market worker."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from backend.market_intelligence_scan import DEFAULT_CANDLE_LIMIT, market_intelligence_scan
from backend.strategy_assessment_engine import MultiStrategyAssessmentEngine


@dataclass(frozen=True, slots=True)
class SearchRunResult:
    scan_id: str
    assessed_at: str
    symbols_scanned: int
    strategy_assessments_count: int
    eligible_candidate_count: int
    analysis_only_count: int
    symbols: tuple[dict[str, Any], ...]
    orders_authorized: bool = False


def continuous_opportunity_search(
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
):
    """Create a scheduled search handler over the worker's existing public source.

    The handler shares the current market-intelligence fetch path; it does not
    construct another exchange client or poller. It durably publishes scan reports
    through the existing shared-market store. Durable opportunity identity and
    waiting-state creation are handled by the opportunity coordinator stage.
    """
    if environment != "TESTNET":
        raise ValueError("continuous opportunity search is Testnet-only")
    if market_type != "FUTURES" or venue != "BINANCE_USDM_TESTNET":
        raise ValueError("continuous opportunity search currently supports Binance USD-M Futures Testnet only")

    scan_handler = market_intelligence_scan(
        store,
        source,
        symbol_policies,
        market_type=market_type,
        environment=environment,
        venue=venue,
        candle_limit=candle_limit,
        max_snapshot_age_seconds=max_snapshot_age_seconds,
        max_fetch_workers=max_fetch_workers,
        strategy_engine=strategy_engine,
    )

    def handle(payload: dict[str, Any], idempotency_key: str) -> SearchRunResult:
        if not isinstance(payload, dict) or payload.get("scope") != "continuous_opportunity_search":
            raise ValueError("continuous opportunity search requires scope=continuous_opportunity_search")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("continuous opportunity search idempotency key is required")
        report = scan_handler({"scope": "market_intelligence"}, idempotency_key)
        if not isinstance(report, dict) or report.get("orders_authorized") is not False:
            raise RuntimeError("shared market scan did not return a safe research report")
        symbols = tuple(report.get("symbols", ()))
        assessments = [
            assessment
            for symbol in symbols
            for assessment in symbol.get("selection", {}).get("strategy_assessments", ())
        ]
        if any(
            not isinstance(item, dict)
            or item.get("orders_authorized") is not False
            or item.get("payload", {}).get("orders_authorized") is not False
            for item in assessments
        ):
            raise RuntimeError("strategy assessment report violated paper-only boundary")
        return SearchRunResult(
            scan_id=report["scan_id"],
            assessed_at=report["assessed_at"],
            symbols_scanned=len(symbols),
            strategy_assessments_count=len(assessments),
            eligible_candidate_count=sum(item.get("decision") == "ELIGIBLE" for item in assessments),
            analysis_only_count=sum(item.get("decision") == "ANALYSIS_ONLY" for item in assessments),
            symbols=symbols,
            orders_authorized=False,
        )

    return handle
