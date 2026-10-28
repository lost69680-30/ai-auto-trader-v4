"""Minimal event contract for connecting independent engines without tight coupling."""
from dataclasses import dataclass
from typing import Any, Literal

EventType = Literal[
    "MARKET_SNAPSHOT", "OPPORTUNITY_CANDIDATE", "STRATEGY_EVIDENCE",
    "SIGNAL_PROPOSED", "SIGNAL_REJECTED", "RISK_APPROVED", "RISK_VETOED",
    "PAPER_ORDER", "ORDER_UPDATE", "POSITION_UPDATE", "SYSTEM_ALERT",
]


@dataclass(frozen=True)
class EngineEvent:
    event_id: str
    event_type: EventType
    occurred_at: float
    source: str
    correlation_id: str
    payload: dict[str, Any]
