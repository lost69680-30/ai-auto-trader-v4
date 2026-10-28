"""Safety-first orchestration from validated signal to paper-order intent.

This is a local integration baseline only: it does not connect exchange feeds,
place exchange orders, simulate fills, or persist records across restarts.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Literal, Protocol

from app.core.models import MarketSnapshot, RiskDecision, RiskLimits, TradeProposal
from app.core.risk_guardian import evaluate_proposal
from backend.signal_validation import SignalValidationResult
from backend.signal_watcher import WatchResult


@dataclass(frozen=True, slots=True)
class OrchestrationRequest:
    proposal: TradeProposal
    market: MarketSnapshot
    validation: SignalValidationResult
    watch_result: WatchResult | None
    limits: RiskLimits
    daily_pnl: float
    open_positions: int
    now: float
    initial_equity: float | None = None


@dataclass(frozen=True, slots=True)
class PaperOrderIntent:
    proposal_id: str
    signal_id: str
    symbol: str
    side: str
    quantity: float
    entry_price: float
    stop_loss: float
    take_profit: float
    recorded_at: float
    status: Literal["PENDING_PAPER_SIMULATION"] = "PENDING_PAPER_SIMULATION"
    orders_authorized: bool = False


class PaperLedger(Protocol):
    def record_intent(self, intent: PaperOrderIntent) -> Literal["RECORDED", "DUPLICATE", "CONFLICT", "BLOCKED_BY_EMERGENCY_STOP"]:
        """Store a paper-only intent idempotently; must never submit an exchange order."""


class InMemoryPaperLedger:
    """Process-local ledger for integration tests; not durable or production-ready."""

    def __init__(self) -> None:
        self._orders: dict[str, PaperOrderIntent] = {}
        self._lock = RLock()

    @property
    def orders(self) -> tuple[PaperOrderIntent, ...]:
        with self._lock:
            return tuple(self._orders.values())

    def record_intent(self, intent: PaperOrderIntent) -> Literal["RECORDED", "DUPLICATE", "CONFLICT"]:
        with self._lock:
            existing = self._orders.get(intent.proposal_id)
            if existing is not None:
                same_order = (
                    existing.proposal_id == intent.proposal_id
                    and existing.signal_id == intent.signal_id
                    and existing.symbol == intent.symbol
                    and existing.side == intent.side
                    and existing.quantity == intent.quantity
                    and existing.entry_price == intent.entry_price
                    and existing.stop_loss == intent.stop_loss
                    and existing.take_profit == intent.take_profit
                )
                return "DUPLICATE" if same_order else "CONFLICT"
            self._orders[intent.proposal_id] = intent
            return "RECORDED"


@dataclass(frozen=True, slots=True)
class OrchestrationResult:
    status: str
    reason: str
    proposal_id: str
    risk_decision: RiskDecision | None = None
    paper_intent: PaperOrderIntent | None = None
    orders_authorized: bool = False


class TradingOrchestrator:
    """Connect validation, watcher trigger, deterministic risk, and paper ledger."""

    def __init__(self, ledger: PaperLedger) -> None:
        self._ledger = ledger

    def process(self, request: OrchestrationRequest) -> OrchestrationResult:
        proposal = request.proposal
        validation = request.validation

        if validation.signal_id is None or not validation.signal_id.strip():
            return self._result("SIGNAL_REJECTED", "Signal validation has no signal id.", proposal.proposal_id)
        if validation.status != "VALID":
            status = "WAIT_RECHECK" if validation.status == "WAIT_RECHECK" else "SIGNAL_REJECTED"
            return self._result(status, "Signal did not pass signal validation.", proposal.proposal_id)
        if validation.symbol.upper() != proposal.symbol.upper():
            return self._result("SIGNAL_REJECTED", "Validated signal symbol does not match proposal.", proposal.proposal_id)
        expected_direction = "LONG" if proposal.side == "LONG" else "SHORT"
        if validation.direction != expected_direction:
            return self._result("SIGNAL_REJECTED", "Validated signal direction does not match proposal.", proposal.proposal_id)
        watch = request.watch_result
        if watch is None:
            return self._result("WAITING_FOR_TRIGGER", "No watcher result was supplied; fail closed.", proposal.proposal_id)
        if watch.status != "TRIGGERED":
            return self._result("WAITING_FOR_TRIGGER", "Signal watcher has not confirmed a trigger.", proposal.proposal_id)
        if watch.orders_authorized:
            return self._result("SIGNAL_REJECTED", "Watcher cannot authorize orders; unexpected authorization flag rejected.", proposal.proposal_id)
        if watch.signal_id != validation.signal_id or watch.symbol.upper() != proposal.symbol.upper():
            return self._result("SIGNAL_REJECTED", "Watcher result does not match the validated signal and proposal.", proposal.proposal_id)

        if bool(getattr(self._ledger, "emergency_stop_active", False)):
            return self._result(
                "EMERGENCY_STOP_ACTIVE",
                "The durable emergency stop is active; no new paper intent may be recorded.",
                proposal.proposal_id,
            )

        daily_pnl = request.daily_pnl
        open_positions = request.open_positions
        # A durable ledger is the source of truth for realized loss and open
        # positions. Missing baseline or report errors fail closed instead of
        # silently trusting request-supplied risk numbers.
        report_method = getattr(self._ledger, "realized_risk_report", None)
        if callable(report_method):
            if request.initial_equity is None:
                return self._result(
                    "RISK_VETOED",
                    "Durable-ledger risk accounting requires a configured initial-equity baseline.",
                    proposal.proposal_id,
                )
            try:
                report = report_method(initial_equity=request.initial_equity, as_of=request.now)
                daily_pnl = report.daily_realized_pnl
                open_positions = len(getattr(self._ledger, "open_positions"))
                mark_report_method = getattr(self._ledger, "mark_to_market_risk_report", None)
                if not callable(mark_report_method):
                    return self._result(
                        "RISK_VETOED",
                        "Durable mark-to-market risk reporting is unavailable; fail closed.",
                        proposal.proposal_id,
                    )
                mtm = mark_report_method(
                    initial_equity=request.initial_equity, as_of=request.now,
                    max_mark_age_seconds=request.limits.max_mark_age_seconds,
                )
                if not mtm.marks_fresh:
                    return self._result(
                        "RISK_VETOED",
                        "Open-position mark data is stale or from the future; no new risk allowed.",
                        proposal.proposal_id,
                    )
                if mtm.drawdown_pct >= request.limits.max_drawdown_pct:
                    return self._result(
                        "RISK_VETOED",
                        "Account mark-to-market drawdown limit reached; no new risk allowed.",
                        proposal.proposal_id,
                    )
            except Exception:
                # Any ledger/database/report failure must fail closed.
                return self._result(
                    "RISK_VETOED",
                    "Ledger-derived risk accounting is unavailable or invalid; fail closed.",
                    proposal.proposal_id,
                )

        decision = evaluate_proposal(
            proposal,
            request.market,
            request.limits,
            daily_pnl=daily_pnl,
            open_positions=open_positions,
            now=request.now,
        )
        if not decision.approved:
            return OrchestrationResult(
                status="RISK_VETOED",
                reason=decision.reason,
                proposal_id=proposal.proposal_id,
                risk_decision=decision,
            )

        intent = PaperOrderIntent(
            proposal_id=proposal.proposal_id,
            signal_id=validation.signal_id,
            symbol=proposal.symbol,
            side=proposal.side,
            quantity=proposal.quantity,
            entry_price=proposal.entry_price,
            stop_loss=proposal.stop_loss,
            take_profit=proposal.take_profit,
            recorded_at=request.now,
        )
        ledger_status = self._ledger.record_intent(intent)
        if ledger_status == "DUPLICATE":
            return OrchestrationResult(
                status="DUPLICATE_IGNORED",
                reason="The same proposal was already recorded; no second intent was created.",
                proposal_id=proposal.proposal_id,
                risk_decision=decision,
            )
        if ledger_status == "CONFLICT":
            return OrchestrationResult(
                status="IDEMPOTENCY_CONFLICT",
                reason="Proposal id already exists with different contents; fail closed.",
                proposal_id=proposal.proposal_id,
                risk_decision=decision,
            )
        if ledger_status == "BLOCKED_BY_EMERGENCY_STOP":
            return OrchestrationResult(
                status="EMERGENCY_STOP_ACTIVE",
                reason="The durable emergency stop blocked this paper-order intent.",
                proposal_id=proposal.proposal_id,
                risk_decision=decision,
            )
        if ledger_status != "RECORDED":
            return OrchestrationResult(
                status="LEDGER_REJECTED",
                reason="Paper ledger returned an unknown status; orchestration failed closed.",
                proposal_id=proposal.proposal_id,
                risk_decision=decision,
            )
        return OrchestrationResult(
            status="PAPER_ORDER_RECORDED",
            reason="Risk approved; paper-only order intent recorded. No exchange order was submitted.",
            proposal_id=proposal.proposal_id,
            risk_decision=decision,
            paper_intent=intent,
        )

    @staticmethod
    def _result(status: str, reason: str, proposal_id: str) -> OrchestrationResult:
        return OrchestrationResult(status=status, reason=reason, proposal_id=proposal_id)
