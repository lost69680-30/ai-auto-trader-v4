"""Deterministic event replay for the local SQLite paper-trading engine.

The replay harness is intentionally offline and paper-only. It accepts explicit,
recorded events; it has no market-data client, exchange credentials, or live-order
capability. Event timestamps are replay-clock timestamps, while market update
``observed_at`` records when the price was observed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, TypeAlias

from app.engines.orchestrator import PaperOrderIntent
from app.engines.paper_ledger import PaperFill, PaperOrderRecord, PaperPosition, SQLitePaperLedger
from app.engines.protection import PaperProtectionEngine


@dataclass(frozen=True, slots=True)
class RecordIntentEvent:
    at: float
    intent: PaperOrderIntent


@dataclass(frozen=True, slots=True)
class FillEvent:
    at: float
    proposal_id: str
    market_price: float
    fee_rate: float = 0.0004
    slippage_bps: float = 2.0


@dataclass(frozen=True, slots=True)
class MarketUpdateEvent:
    at: float
    observed_at: float
    prices: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class EmergencyStopEvent:
    at: float
    reason: str
    prices: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RestartEvent:
    at: float


ReplayEvent: TypeAlias = (
    RecordIntentEvent | FillEvent | MarketUpdateEvent | EmergencyStopEvent | RestartEvent
)


@dataclass(frozen=True, slots=True)
class ReplayStep:
    index: int
    event_type: str
    at: float
    status: str
    reason: str
    orders_authorized: bool = False


@dataclass(frozen=True, slots=True)
class ReplayReport:
    steps: tuple[ReplayStep, ...]
    orders: tuple[PaperOrderRecord, ...]
    fills: tuple[PaperFill, ...]
    positions: tuple[PaperPosition, ...]
    open_positions: tuple[PaperPosition, ...]
    emergency_stop_active: bool
    orders_authorized: bool = False


class PaperReplayHarness:
    """Replay ordered test events against a durable paper ledger.

    Stale, future-dated, and out-of-order market updates are rejected before they
    can alter marks or trigger simulated exits. A restart closes and reopens the
    SQLite connection to exercise actual persisted state recovery.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        max_market_age_seconds: float = 5.0,
        fee_rate: float = 0.0004,
        slippage_bps: float = 2.0,
        trailing_distance_pct: float | None = None,
        trailing_activation_pct: float = 0.0,
    ) -> None:
        path = str(database_path)
        if not path.strip() or path == ":memory:":
            raise ValueError("Replay requires a file-backed SQLite database to test restart recovery.")
        if not math.isfinite(max_market_age_seconds) or max_market_age_seconds < 0:
            raise ValueError("max_market_age_seconds must be finite and non-negative.")
        self.database_path = path
        self.max_market_age_seconds = max_market_age_seconds
        self.fee_rate = fee_rate
        self.slippage_bps = slippage_bps
        self.trailing_distance_pct = trailing_distance_pct
        self.trailing_activation_pct = trailing_activation_pct

    def run(self, events: tuple[ReplayEvent, ...] | list[ReplayEvent]) -> ReplayReport:
        ledger = SQLitePaperLedger(self.database_path)
        protection = self._protection_engine(ledger)
        steps: list[ReplayStep] = []
        last_event_at: float | None = None
        last_market_observed_at: float | None = None
        try:
            for index, event in enumerate(events):
                event_type = type(event).__name__
                at = event.at
                if not math.isfinite(at) or at < 0:
                    steps.append(ReplayStep(index, event_type, at, "INVALID_EVENT_TIME_REJECTED", "Event time must be finite and non-negative."))
                    continue
                if last_event_at is not None and at < last_event_at:
                    steps.append(ReplayStep(index, event_type, at, "OUT_OF_ORDER_EVENT_REJECTED", "Replay event time moved backwards; ledger was not mutated."))
                    continue
                last_event_at = at

                if isinstance(event, RecordIntentEvent):
                    if event.intent.recorded_at > at:
                        steps.append(ReplayStep(index, event_type, at, "FUTURE_INTENT_REJECTED", "Intent timestamp is later than the replay event."))
                        continue
                    status = ledger.record_intent(event.intent)
                    steps.append(ReplayStep(index, event_type, at, status, f"Paper intent recording returned {status}."))
                elif isinstance(event, FillEvent):
                    result = ledger.simulate_fill(
                        event.proposal_id,
                        market_price=event.market_price,
                        filled_at=at,
                        fee_rate=event.fee_rate,
                        slippage_bps=event.slippage_bps,
                    )
                    steps.append(ReplayStep(index, event_type, at, result.status, result.reason))
                elif isinstance(event, MarketUpdateEvent):
                    if not math.isfinite(event.observed_at) or event.observed_at < 0:
                        steps.append(ReplayStep(index, event_type, at, "INVALID_MARKET_TIME_REJECTED", "Market observation time must be finite and non-negative."))
                        continue
                    if event.observed_at > at:
                        steps.append(ReplayStep(index, event_type, at, "FUTURE_MARKET_REJECTED", "Market observation timestamp is ahead of the replay clock."))
                        continue
                    if at - event.observed_at > self.max_market_age_seconds:
                        steps.append(ReplayStep(index, event_type, at, "STALE_MARKET_REJECTED", "Market observation exceeded the configured maximum age."))
                        continue
                    if last_market_observed_at is not None and event.observed_at < last_market_observed_at:
                        steps.append(ReplayStep(index, event_type, at, "OUT_OF_ORDER_MARKET_REJECTED", "Market observation timestamp moved backwards."))
                        continue
                    result = protection.process_market_update(event.prices, observed_at=event.observed_at)
                    last_market_observed_at = event.observed_at
                    steps.append(ReplayStep(index, event_type, at, result.status, result.reason))
                elif isinstance(event, EmergencyStopEvent):
                    result = protection.activate_emergency_stop(
                        observed_at=at,
                        reason=event.reason,
                        market_prices=event.prices or None,
                    )
                    steps.append(ReplayStep(index, event_type, at, "EMERGENCY_STOP_ACTIVE", result.reason))
                elif isinstance(event, RestartEvent):
                    ledger.close()
                    ledger = SQLitePaperLedger(self.database_path)
                    protection = self._protection_engine(ledger)
                    steps.append(ReplayStep(index, event_type, at, "RESTARTED", "Reopened the SQLite ledger and rebuilt protection engine from persisted state."))
                else:  # Defensive guard if a caller bypasses the declared event union.
                    steps.append(ReplayStep(index, event_type, at, "UNKNOWN_EVENT_REJECTED", "Unsupported replay event type."))

            return ReplayReport(
                steps=tuple(steps),
                orders=ledger.orders,
                fills=ledger.fills,
                positions=ledger.positions,
                open_positions=ledger.open_positions,
                emergency_stop_active=ledger.emergency_stop_active,
                orders_authorized=False,
            )
        finally:
            ledger.close()

    def _protection_engine(self, ledger: SQLitePaperLedger) -> PaperProtectionEngine:
        return PaperProtectionEngine(
            ledger,
            fee_rate=self.fee_rate,
            slippage_bps=self.slippage_bps,
            trailing_distance_pct=self.trailing_distance_pct,
            trailing_activation_pct=self.trailing_activation_pct,
        )
