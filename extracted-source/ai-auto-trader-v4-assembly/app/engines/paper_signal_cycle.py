"""Connect validated signal handoff, trigger watcher, risk checks and paper ledger.

This coordinator is paper-only. It consumes an already validated handoff and an
explicitly sized orchestration request; it does not invent research or prices and
has no exchange execution capability.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import math

from app.core.models import MarketSnapshot
from app.engines.orchestrator import OrchestrationRequest, OrchestrationResult, TradingOrchestrator
from app.engines.signal_handoff import SignalHandoffResult
from backend.signal_watcher import MarketTick, SignalWatcher, WatchRequest


@dataclass(frozen=True, slots=True)
class PaperSignalCycleResult:
    status: str
    reason: str
    orchestration: OrchestrationResult | None = None
    orders_authorized: bool = False


class PaperSignalCycle:
    """Run one signal through watch-trigger, revalidation boundary and risk gate."""

    def __init__(self, orchestrator: TradingOrchestrator, watcher: SignalWatcher) -> None:
        self._orchestrator = orchestrator
        self._watcher = watcher
        self._registered: dict[str, WatchRequest] = {}

    def process(
        self,
        handoff: SignalHandoffResult,
        request: OrchestrationRequest,
        tick: MarketTick,
        *,
        now: datetime,
    ) -> PaperSignalCycleResult:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            return self._reject("cycle clock must be timezone-aware")
        now = now.astimezone(timezone.utc)
        if handoff.orders_authorized or handoff.status != "VALID":
            return self._reject("signal handoff is not valid and paper-only")
        if handoff.signal is None or handoff.validation is None or handoff.watch_request is None:
            return self._reject("validated signal, validation result, or watch request is missing")
        if handoff.validation.status != "VALID" or handoff.validation.orders_authorized:
            return self._reject("signal validation is invalid or claims order authorization")
        signal, watch_request, proposal = handoff.signal, handoff.watch_request, request.proposal
        if request.validation.orders_authorized:
            return self._reject("request validation unexpectedly claims order authorization")
        if tick.symbol != signal.symbol or request.market.symbol != signal.symbol or proposal.symbol != signal.symbol:
            return self._reject("tick, market snapshot, signal, and proposal symbols must match")
        if tick.observed_at.tzinfo is None or tick.observed_at.utcoffset() is None:
            return self._reject("tick timestamp must be timezone-aware")
        if tick.observed_at > now:
            return self._reject("tick is future-dated")
        if not math.isfinite(tick.price) or tick.price <= 0:
            return self._reject("tick price must be finite and positive")
        if proposal.side != signal.direction:
            return self._reject("proposal side does not match validated signal direction")
        if not (
            math.isclose(proposal.entry_price, signal.entry_price, rel_tol=1e-9, abs_tol=1e-9)
            and math.isclose(proposal.stop_loss, signal.stop_price, rel_tol=1e-9, abs_tol=1e-9)
            and math.isclose(proposal.take_profit, signal.target_price, rel_tol=1e-9, abs_tol=1e-9)
        ):
            return self._reject("proposal entry, stop, or target differs from validated signal prices")
        if watch_request.signal_id != signal.signal_id or watch_request.symbol != signal.symbol:
            return self._reject("watch request does not match validated signal")
        existing = self._registered.get(watch_request.watch_id)
        if existing is None:
            try:
                self._watcher.add(watch_request)
            except (ValueError, TypeError):
                return self._reject("watch registration failed; no paper processing performed")
            self._registered[watch_request.watch_id] = watch_request
        elif existing != watch_request:
            return self._reject("watch id was reused with conflicting contents")

        try:
            watch_results = self._watcher.evaluate_all(tick, now=now)
        except (ValueError, TypeError):
            return self._reject("watcher rejected the market tick")
        matches = [result for result in watch_results if result.watch_id == watch_request.watch_id]
        if len(matches) != 1:
            return self._reject("watcher did not return exactly one result for this watch")
        watch_result = matches[0]
        if watch_result.orders_authorized:
            return self._reject("watcher unexpectedly claims order authorization")

        market = MarketSnapshot(
            symbol=tick.symbol,
            mark_price=tick.price,
            observed_at=tick.observed_at.astimezone(timezone.utc).timestamp(),
            is_fresh=True,
        )
        orchestration_request = replace(
            request,
            market=market,
            validation=handoff.validation,
            watch_result=watch_result,
            now=now.timestamp(),
        )
        result = self._orchestrator.process(orchestration_request)
        return PaperSignalCycleResult(result.status, result.reason, result, False)

    @staticmethod
    def _reject(reason: str) -> PaperSignalCycleResult:
        return PaperSignalCycleResult("PAPER_CYCLE_REJECTED", reason, None, False)
