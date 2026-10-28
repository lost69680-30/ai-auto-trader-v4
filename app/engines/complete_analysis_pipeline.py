"""End-to-end coordinator from completed research to paper-only risk orchestration.

The market adapter/worker and ``analyze_research_universe`` remain explicit inputs:
this coordinator connects their output to ATR planning, signal validation, trigger
watching, risk checks and paper intent recording. It cannot submit exchange orders.
Position sizing is deliberately delegated to a caller-supplied factory and then
independently checked by the Risk Guardian.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Mapping

from app.engines.orchestrator import OrchestrationRequest
from app.engines.price_planner import PricePlanRequest, PricePlanningConfig, plan_prices
from app.engines.signal_handoff import prepare_signal
from app.engines.paper_signal_cycle import PaperSignalCycle
from backend.signal_watcher import MarketTick


@dataclass(frozen=True, slots=True)
class AnalysisPipelineItem:
    symbol: str
    status: str
    reason: str
    research_status: str
    plan_status: str | None = None
    signal_status: str | None = None
    paper_status: str | None = None
    orders_authorized: bool = False


@dataclass(frozen=True, slots=True)
class AnalysisPipelineReport:
    status: str
    items: tuple[AnalysisPipelineItem, ...]
    orders_authorized: bool = False


class CompleteAnalysisPipeline:
    """Bridge research results into the existing fully checked paper signal cycle."""

    def __init__(
        self,
        *,
        plan_request_factory: Callable[[object], PricePlanRequest],
        orchestration_request_factory: Callable[[object, object], OrchestrationRequest],
        signal_cycle: PaperSignalCycle,
        price_config: PricePlanningConfig | None = None,
    ) -> None:
        self._plan_request_factory = plan_request_factory
        self._request_factory = orchestration_request_factory
        self._cycle = signal_cycle
        self._price_config = price_config

    def process_research(
        self,
        research_result: object,
        *,
        ticks: Mapping[str, MarketTick],
        now: datetime,
    ) -> AnalysisPipelineReport:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if getattr(research_result, "orders_authorized", False):
            return AnalysisPipelineReport("RESEARCH_AUTHORIZATION_FLAG_REJECTED", (), False)
        items: list[AnalysisPipelineItem] = []
        seen: set[str] = set()
        for candidate in getattr(research_result, "candidates", ()):
            symbol = getattr(candidate, "symbol", "")
            if not isinstance(symbol, str) or not symbol or symbol != symbol.strip().upper():
                items.append(AnalysisPipelineItem(str(symbol), "CANDIDATE_REJECTED", "Candidate symbol is not normalized.", str(getattr(candidate, "status", "UNKNOWN"))))
                continue
            if symbol in seen:
                items.append(AnalysisPipelineItem(symbol, "DUPLICATE_CANDIDATE_REJECTED", "Duplicate symbol in research result.", str(candidate.status)))
                continue
            seen.add(symbol)
            if getattr(candidate, "orders_authorized", False):
                items.append(AnalysisPipelineItem(symbol, "CANDIDATE_AUTHORIZATION_FLAG_REJECTED", "Research candidate cannot authorize orders.", str(candidate.status)))
                continue
            if getattr(candidate, "status", None) != "REVIEW" or getattr(candidate, "direction", None) not in ("LONG", "SHORT"):
                items.append(AnalysisPipelineItem(symbol, "NO_TRADE", "Research engines did not agree on an actionable review candidate.", str(getattr(candidate, "status", "UNKNOWN"))))
                continue
            tick = ticks.get(symbol)
            if tick is None:
                items.append(AnalysisPipelineItem(symbol, "TICK_MISSING", "No current market tick was supplied.", str(candidate.status)))
                continue
            try:
                plan_request = self._plan_request_factory(candidate)
                if plan_request.symbol != symbol or plan_request.direction != candidate.direction:
                    items.append(AnalysisPipelineItem(symbol, "PLAN_INPUT_MISMATCH", "Price-planner request does not match research candidate.", str(candidate.status)))
                    continue
                planned = plan_prices(plan_request, self._price_config)
                if planned.status != "READY" or planned.plan is None:
                    items.append(AnalysisPipelineItem(symbol, "NO_PLAN", planned.reason, str(candidate.status), planned.status))
                    continue
                handoff = prepare_signal(candidate, planned.plan, now=now)
                if handoff.status != "VALID" or handoff.watch_request is None:
                    items.append(AnalysisPipelineItem(symbol, "SIGNAL_REJECTED", handoff.reason, str(candidate.status), planned.status, handoff.status))
                    continue
                request = self._request_factory(candidate, handoff)
                cycle_result = self._cycle.process(handoff, request, tick, now=now)
                orchestration = getattr(cycle_result, "orchestration", None)
                paper_status = orchestration.status if orchestration is not None else cycle_result.status
                items.append(AnalysisPipelineItem(
                    symbol, paper_status, cycle_result.reason, str(candidate.status),
                    planned.status, handoff.status, paper_status, False,
                ))
            except Exception as exc:
                # Do not leak exception text, request contents, or credentials to reports.
                items.append(AnalysisPipelineItem(
                    symbol, "PIPELINE_ERROR", f"Pipeline stage failed safely ({type(exc).__name__}).",
                    str(getattr(candidate, "status", "UNKNOWN")), orders_authorized=False,
                ))
        return AnalysisPipelineReport("PROCESSED" if items else "NO_CANDIDATES", tuple(items), False)
