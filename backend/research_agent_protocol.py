"""Fail-closed contracts for deterministic read-only specialist research.

This supervisor runs registered local analysis handlers over one immutable,
fingerprinted market snapshot. It is not an autonomous LLM-agent bus, does not
authorize paper intents, and cannot modify risk/execution settings.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any


_SPECIALIST_ROLES = frozenset({
    "market_observer",
    "technical_analyst",
    "regime_analyst",
    "liquidity_analyst",
    "volatility_analyst",
    "pattern_analyst",
    "multi_timeframe_analyst",
    "strategy_researcher",
    "opportunity_evaluator",
    "performance_auditor",
    "data_integrity_guardian",
    "portfolio_analyst",
    "anomaly_detector",
})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_PAYLOAD_KEYS = frozenset({
    "order",
    "order_id",
    "order_payload",
    "submit_order",
    "place_order",
    "execution",
    "paper_intent",
    "risk_limit",
    "risk_limits",
    "risk_settings",
    "leverage",
    "position_size",
    "quantity",
    "notional",
    "capital_allocation",
    "emergency_stop",
    "orders_authorized",
})


def _aware(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _text(value: object, name: str, maximum: int = 160) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be trimmed non-empty text")
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _validate_payload(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("specialist payload must be a mapping")
    plain = dict(value)

    def inspect_keys(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str) or not key.strip():
                    raise ValueError("specialist payload keys must be non-empty strings")
                normalized = key.strip().lower()
                if normalized in _FORBIDDEN_PAYLOAD_KEYS:
                    raise ValueError("specialist payload cannot contain risk or execution controls")
                inspect_keys(child)
        elif isinstance(item, (tuple, list)):
            for child in item:
                inspect_keys(child)

    inspect_keys(plain)
    try:
        encoded = json.dumps(plain, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("specialist payload must contain finite JSON-safe evidence") from exc
    if len(encoded.encode("utf-8")) > 65536:
        raise ValueError("specialist payload exceeds 64 KiB")
    return _freeze(plain)


def _validate_role(value: object) -> str:
    role = _text(value, "specialist role", 80).lower()
    if role not in _SPECIALIST_ROLES:
        raise ValueError("unsupported specialist role")
    return role


@dataclass(frozen=True, slots=True)
class ResearchAgentInput:
    symbol: str
    market_type: str
    venue: str
    data_fingerprint: str
    observed_at: datetime
    expires_at: datetime
    orders_authorized: bool = False

    def __post_init__(self) -> None:
        symbol = _text(self.symbol, "symbol", 40)
        if symbol != symbol.upper() or not symbol.isalnum():
            raise ValueError("symbol must be canonical uppercase alphanumeric text")
        market_type = _text(self.market_type, "market_type", 20).upper()
        if market_type not in {"SPOT", "FUTURES"}:
            raise ValueError("market_type must be SPOT or FUTURES")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "market_type", market_type)
        object.__setattr__(self, "venue", _text(self.venue, "venue", 100))
        if not isinstance(self.data_fingerprint, str) or not _SHA256.fullmatch(self.data_fingerprint):
            raise ValueError("data_fingerprint must be a lowercase SHA-256 digest")
        observed = _aware(self.observed_at, "observed_at")
        expires = _aware(self.expires_at, "expires_at")
        if expires <= observed:
            raise ValueError("snapshot expiry must be after its observation time")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "expires_at", expires)
        if self.orders_authorized is not False:
            raise ValueError("orders_authorized must be false")


@dataclass(frozen=True, slots=True)
class AgentEvidenceMessage:
    message_id: str
    agent_id: str
    role: str
    symbol: str
    market_type: str
    venue: str
    data_fingerprint: str
    observed_at: datetime
    expires_at: datetime
    status: str
    payload: Mapping[str, Any]
    error_code: str | None = None
    orders_authorized: bool = False

    def __post_init__(self) -> None:
        _text(self.message_id, "message_id", 128)
        _text(self.agent_id, "agent_id", 120)
        object.__setattr__(self, "role", _validate_role(self.role))
        if self.status not in {"COMPLETE", "INCOMPLETE", "FAILED"}:
            raise ValueError("specialist status is invalid")
        observed = _aware(self.observed_at, "message observed_at")
        expires = _aware(self.expires_at, "message expires_at")
        if expires <= observed:
            raise ValueError("specialist message expiry must be after observation time")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "expires_at", expires)
        if not isinstance(self.data_fingerprint, str) or not _SHA256.fullmatch(self.data_fingerprint):
            raise ValueError("specialist data fingerprint is invalid")
        object.__setattr__(self, "payload", _validate_payload(self.payload))
        if self.status == "FAILED":
            if self.payload or self.error_code is None:
                raise ValueError("failed specialist messages require an empty payload and error code")
        elif self.error_code is not None:
            raise ValueError("successful or incomplete specialist messages cannot carry error codes")
        if self.error_code is not None:
            _text(self.error_code, "error_code", 80)
        if self.orders_authorized is not False:
            raise ValueError("orders_authorized must be false")


@dataclass(frozen=True, slots=True)
class ResearchAgentRun:
    status: str
    symbol: str
    data_fingerprint: str
    required_roles: tuple[str, ...]
    missing_roles: tuple[str, ...]
    messages: tuple[AgentEvidenceMessage, ...]
    orders_authorized: bool = False

    def __post_init__(self) -> None:
        if self.status not in {"COMPLETE", "INCOMPLETE", "FAILED"}:
            raise ValueError("research agent run status is invalid")
        if not isinstance(self.required_roles, tuple) or not isinstance(self.missing_roles, tuple):
            raise ValueError("agent role lists must be immutable tuples")
        if not self.required_roles:
            raise ValueError("research agent run requires at least one specialist role")
        if len(self.required_roles) != len(set(self.required_roles)):
            raise ValueError("required specialist roles must be unique")
        if len(self.messages) != len({message.message_id for message in self.messages}):
            raise ValueError("specialist message IDs must be unique")
        if len(self.messages) != len({message.role for message in self.messages}):
            raise ValueError("specialist roles may produce only one message per run")
        if any(
            message.symbol != self.symbol
            or message.data_fingerprint != self.data_fingerprint
            or message.orders_authorized is not False
            for message in self.messages
        ):
            raise ValueError("specialist messages do not share the run's paper-only market snapshot")
        if not set(self.missing_roles).issubset(set(self.required_roles)):
            raise ValueError("missing specialist roles must belong to the required role set")
        if self.orders_authorized is not False:
            raise ValueError("orders_authorized must be false")


def execute_specialist_agents(
    snapshot: ResearchAgentInput,
    handlers: Mapping[str, Callable[[ResearchAgentInput], Mapping[str, Any]]],
    *,
    required_roles: tuple[str, ...],
    as_of: datetime,
    max_age_seconds: float = 60.0,
) -> ResearchAgentRun:
    """Run allowlisted read-only handlers and assemble immutable evidence messages.

    Handler exceptions and invalid payloads become sanitized FAILED messages.
    The supervisor never combines scores into a trade decision or invokes Risk Guardian.
    """
    if not isinstance(snapshot, ResearchAgentInput) or snapshot.orders_authorized is not False:
        raise ValueError("a paper-only research agent input is required")
    now = _aware(as_of, "as_of")
    if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, (int, float)):
        raise ValueError("max_age_seconds must be a positive finite number")
    max_age = float(max_age_seconds)
    if not math.isfinite(max_age) or max_age <= 0:
        raise ValueError("max_age_seconds must be a positive finite number")
    if snapshot.observed_at > now or now > snapshot.expires_at or (now - snapshot.observed_at).total_seconds() > max_age:
        raise ValueError("research snapshot is expired or stale")
    if not isinstance(handlers, Mapping):
        raise ValueError("specialist handlers must be a role-to-callable mapping")
    if len(handlers) > len(_SPECIALIST_ROLES):
        raise ValueError("too many specialist handlers")
    normalized_handlers: dict[str, Callable] = {}
    for raw_role, handler in handlers.items():
        role = _validate_role(raw_role)
        if role in normalized_handlers or not callable(handler):
            raise ValueError("specialist handlers must have unique roles and callable values")
        normalized_handlers[role] = handler
    if not isinstance(required_roles, tuple):
        raise ValueError("required_roles must be an immutable tuple")
    roles = tuple(_validate_role(role) for role in required_roles)
    if not roles:
        raise ValueError("required_roles must contain at least one specialist role")
    if len(roles) != len(set(roles)):
        raise ValueError("required specialist roles must be unique")

    expiry = min(snapshot.expires_at, now + timedelta(seconds=max_age))
    messages: list[AgentEvidenceMessage] = []
    for role in sorted(normalized_handlers):
        message_id = hashlib.sha256(
            f"{snapshot.symbol}|{snapshot.data_fingerprint}|{role}|{now.isoformat()}".encode("utf-8")
        ).hexdigest()
        agent_id = f"specialist:{role}"
        try:
            payload = normalized_handlers[role](snapshot)
            message = AgentEvidenceMessage(
                message_id=message_id,
                agent_id=agent_id,
                role=role,
                symbol=snapshot.symbol,
                market_type=snapshot.market_type,
                venue=snapshot.venue,
                data_fingerprint=snapshot.data_fingerprint,
                observed_at=now,
                expires_at=expiry,
                status="COMPLETE",
                payload=payload,
                orders_authorized=False,
            )
        except Exception as exc:
            message = AgentEvidenceMessage(
                message_id=message_id,
                agent_id=agent_id,
                role=role,
                symbol=snapshot.symbol,
                market_type=snapshot.market_type,
                venue=snapshot.venue,
                data_fingerprint=snapshot.data_fingerprint,
                observed_at=now,
                expires_at=expiry,
                status="FAILED",
                payload={},
                error_code=type(exc).__name__,
                orders_authorized=False,
            )
        messages.append(message)

    available = {message.role for message in messages}
    missing = tuple(sorted(set(roles) - available))
    if any(message.status == "FAILED" for message in messages):
        status = "FAILED"
    elif missing or any(message.status == "INCOMPLETE" for message in messages):
        status = "INCOMPLETE"
    else:
        status = "COMPLETE"
    return ResearchAgentRun(
        status=status,
        symbol=snapshot.symbol,
        data_fingerprint=snapshot.data_fingerprint,
        required_roles=tuple(sorted(roles)),
        missing_roles=missing,
        messages=tuple(messages),
        orders_authorized=False,
    )
