"""Durable, paper-only PostgreSQL opportunity registry and audit log."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from typing import Any, Iterable

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - deployment dependency
    psycopg = None
    dict_row = None

from app.engines.postgres_ssl import postgres_connection_ssl_options


_STATES = {
    "DISCOVERED", "ASSESSING", "WAITING", "REASSESSING", "TRIGGERED",
    "RISK_CHECK", "INTENT_RECORDED", "PAPER_OPEN", "REJECTED",
    "INVALIDATED", "EXPIRED", "CANCELLED", "BLOCKED", "NO_TRADE",
}
_TERMINAL_STATES = {"PAPER_OPEN", "REJECTED", "INVALIDATED", "EXPIRED", "CANCELLED", "BLOCKED", "NO_TRADE"}
_ALLOWED_TRANSITIONS = {
    "DISCOVERED": {"ASSESSING", "REJECTED", "BLOCKED", "NO_TRADE", "CANCELLED"},
    "ASSESSING": {"WAITING", "REJECTED", "BLOCKED", "NO_TRADE", "CANCELLED"},
    "WAITING": {"REASSESSING", "TRIGGERED", "INVALIDATED", "EXPIRED", "BLOCKED", "CANCELLED"},
    "REASSESSING": {"WAITING", "TRIGGERED", "INVALIDATED", "EXPIRED", "BLOCKED", "NO_TRADE", "CANCELLED"},
    "TRIGGERED": {"RISK_CHECK", "EXPIRED", "BLOCKED", "CANCELLED", "NO_TRADE"},
    "RISK_CHECK": {"INTENT_RECORDED", "BLOCKED", "NO_TRADE", "CANCELLED"},
    "INTENT_RECORDED": {"PAPER_OPEN", "BLOCKED"},
}
_STRATEGIES = {"scalping", "day", "swing", "position", "long_term"}
_VENUES = {"FUTURES": "BINANCE_USDM_TESTNET", "SPOT": "BINANCE_SPOT_TESTNET"}


def _utc(value: datetime | None, *, field_name: str = "timestamp") -> datetime:
    stamp = value or datetime.now(timezone.utc)
    if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return stamp.astimezone(timezone.utc)


def _required_text(value: str, field_name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum:
        raise ValueError(f"{field_name} must be a trimmed string of 1 to {maximum} characters")
    return value


def _assert_paper_only(value: Any, path: str = "payload") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "orders_authorized" and item is not False:
                raise ValueError(f"{path}.orders_authorized must be false")
            _assert_paper_only(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_paper_only(item, f"{path}[{index}]")


def _json(value: Any, field_name: str) -> str:
    _assert_paper_only(value, field_name)
    try:
        return json.dumps(value, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must contain valid JSON values") from exc


@dataclass(frozen=True)
class OpportunityRecord:
    opportunity_id: str
    symbol: str
    market_type: str
    venue: str
    strategy_id: str
    strategy_version: str
    setup_fingerprint: str
    direction: str
    timeframe: str
    state: str = "DISCOVERED"
    version: int = 0
    expires_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    orders_authorized: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        _required_text(self.opportunity_id, "opportunity_id", 160)
        if self.orders_authorized is not False:
            raise ValueError("orders_authorized must be false")
        if self.symbol != self.symbol.strip().upper() or not 1 <= len(self.symbol) <= 30:
            raise ValueError("symbol must be trimmed uppercase text")
        if self.market_type not in _VENUES or self.venue != _VENUES[self.market_type]:
            raise ValueError("market_type and Testnet venue do not match")
        if self.strategy_id not in _STRATEGIES:
            raise ValueError("strategy_id is not supported")
        _required_text(self.strategy_version, "strategy_version", 80)
        _required_text(self.setup_fingerprint, "setup_fingerprint", 240)
        if self.direction not in {"LONG", "SHORT", "NEUTRAL"}:
            raise ValueError("direction is invalid")
        _required_text(self.timeframe, "timeframe", 12)
        if self.state not in _STATES:
            raise ValueError("opportunity state is invalid")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 0:
            raise ValueError("version must be a non-negative integer")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata must be a JSON object")
        _assert_paper_only(self.metadata, "metadata")
        if self.expires_at is not None:
            _utc(self.expires_at, field_name="expires_at")


@dataclass(frozen=True)
class StrategyAssessmentRecord:
    assessment_id: str
    opportunity_id: str
    strategy_id: str
    strategy_version: str
    assessment_version: int
    decision: str
    payload: dict[str, Any]
    evidence_refs: list[str]
    orders_authorized: bool = False

    def __post_init__(self) -> None:
        if self.orders_authorized is not False:
            raise ValueError("orders_authorized must be false")
        _required_text(self.assessment_id, "assessment_id", 160)
        _required_text(self.opportunity_id, "opportunity_id", 160)
        if self.strategy_id not in _STRATEGIES:
            raise ValueError("strategy_id is not supported")
        _required_text(self.strategy_version, "strategy_version", 80)
        if isinstance(self.assessment_version, bool) or not isinstance(self.assessment_version, int) or self.assessment_version < 1:
            raise ValueError("assessment_version must be a positive integer")
        if self.decision not in {"WAIT", "REJECT", "REQUEST_MORE_EVIDENCE", "ANALYSIS_ONLY", "NO_TRADE", "ELIGIBLE"}:
            raise ValueError("assessment decision is invalid")
        if not isinstance(self.payload, dict) or not isinstance(self.evidence_refs, list):
            raise ValueError("assessment payload and evidence_refs have invalid types")
        for ref in self.evidence_refs:
            _required_text(ref, "evidence_ref", 240)


@dataclass(frozen=True)
class OpportunityEvidenceRecord:
    evidence_id: str
    opportunity_id: str
    evidence_type: str
    reference_id: str
    observed_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)
    orders_authorized: bool = False

    def __post_init__(self) -> None:
        if self.orders_authorized is not False:
            raise ValueError("orders_authorized must be false")
        _required_text(self.evidence_id, "evidence_id", 160)
        _required_text(self.opportunity_id, "opportunity_id", 160)
        _required_text(self.evidence_type, "evidence_type", 60)
        _required_text(self.reference_id, "reference_id", 240)
        _utc(self.observed_at, field_name="observed_at")
        if not isinstance(self.payload, dict):
            raise ValueError("evidence payload must be a JSON object")


@dataclass(frozen=True)
class WatchConditionRecord:
    watch_id: str
    opportunity_id: str
    trigger_rule: str
    trigger_price: float
    invalidation_price: float | None
    timeframe: str
    expires_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)
    orders_authorized: bool = False

    def __post_init__(self) -> None:
        if self.orders_authorized is not False:
            raise ValueError("orders_authorized must be false")
        _required_text(self.watch_id, "watch_id", 160)
        _required_text(self.opportunity_id, "opportunity_id", 160)
        if self.trigger_rule not in {"AT_OR_ABOVE", "AT_OR_BELOW", "CANDLE_CLOSE_ABOVE", "CANDLE_CLOSE_BELOW"}:
            raise ValueError("trigger_rule is invalid")
        if isinstance(self.trigger_price, bool) or not isinstance(self.trigger_price, (int, float)) or not math.isfinite(self.trigger_price) or self.trigger_price <= 0:
            raise ValueError("trigger_price must be finite and positive")
        if self.invalidation_price is not None and (
            isinstance(self.invalidation_price, bool)
            or not isinstance(self.invalidation_price, (int, float))
            or not math.isfinite(self.invalidation_price)
            or self.invalidation_price <= 0
        ):
            raise ValueError("invalidation_price must be finite and positive")
        _required_text(self.timeframe, "timeframe", 12)
        _utc(self.expires_at, field_name="expires_at")
        if not isinstance(self.payload, dict):
            raise ValueError("watch payload must be a JSON object")


@dataclass(frozen=True)
class OpportunityEventRecord:
    event_id: str
    opportunity_id: str
    from_state: str | None
    to_state: str
    opportunity_version: int
    reason: str
    actor: str
    evidence_version: str | None
    payload: dict[str, Any]
    occurred_at: datetime
    orders_authorized: bool = False


@dataclass(frozen=True)
class OpportunityPage:
    items: tuple[OpportunityRecord, ...]
    next_cursor: str | None = None


@dataclass(frozen=True)
class TransitionResult:
    opportunity: OpportunityRecord
    event: OpportunityEventRecord
    duplicate: bool = False


class PostgresOpportunityStore:
    """Persist independent opportunities without granting order authority."""

    def __init__(self, dsn: str) -> None:
        if psycopg is None:
            raise RuntimeError("Opportunity registry requires psycopg.")
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("PAPER_DATABASE_URL is required for durable opportunities")
        self._dsn = dsn.strip()

    def _connect(self):
        return psycopg.connect(
            self._dsn,
            connect_timeout=10,
            row_factory=dict_row,
            **postgres_connection_ssl_options(self._dsn),
        )

    @staticmethod
    def _idempotency(value: str) -> str:
        return _required_text(value, "idempotency_key", 200)

    @staticmethod
    def _opportunity(row: dict[str, Any]) -> OpportunityRecord:
        return OpportunityRecord(
            opportunity_id=row["opportunity_id"],
            symbol=row["symbol"],
            market_type=row["market_type"],
            venue=row["venue"],
            strategy_id=row["strategy_id"],
            strategy_version=row["strategy_version"],
            setup_fingerprint=row["setup_fingerprint"],
            direction=row["direction"],
            timeframe=row["timeframe"],
            state=row["state"],
            version=row["version"],
            expires_at=row["expires_at"],
            metadata=row["metadata"],
            orders_authorized=row["orders_authorized"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _event(row: dict[str, Any]) -> OpportunityEventRecord:
        return OpportunityEventRecord(
            event_id=row["event_id"],
            opportunity_id=row["opportunity_id"],
            from_state=row["from_state"],
            to_state=row["to_state"],
            opportunity_version=row["opportunity_version"],
            reason=row["reason"],
            actor=row["actor"],
            evidence_version=row["evidence_version"],
            payload=row["payload"],
            occurred_at=row["occurred_at"],
            orders_authorized=row["orders_authorized"],
        )

    def create_or_get_opportunity(
        self, record: OpportunityRecord, *, idempotency_key: str,
        now: datetime | None = None,
    ) -> OpportunityRecord:
        if not isinstance(record, OpportunityRecord) or record.orders_authorized is not False:
            raise ValueError("opportunity record must deny order authorization")
        key = self._idempotency(idempotency_key)
        stamp = _utc(now)
        metadata = _json(record.metadata, "metadata")
        with self._connect() as conn:
            inserted = conn.execute(
                """INSERT INTO paper_ledger.opportunities
                   (opportunity_id, idempotency_key, symbol, market_type, venue,
                    strategy_id, strategy_version, setup_fingerprint, direction,
                    timeframe, state, version, expires_at, metadata,
                    orders_authorized, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s::jsonb, false, %s, %s)
                   ON CONFLICT DO NOTHING RETURNING *""",
                (record.opportunity_id, key, record.symbol, record.market_type,
                 record.venue, record.strategy_id, record.strategy_version,
                 record.setup_fingerprint, record.direction, record.timeframe,
                 record.state, record.version, record.expires_at, metadata, stamp, stamp),
            ).fetchone()
            if inserted is not None:
                event_id = "created-" + hashlib.sha256(key.encode("utf-8")).hexdigest()
                conn.execute(
                    """INSERT INTO paper_ledger.opportunity_events
                       (event_id, opportunity_id, from_state, to_state, reason,
                        actor, evidence_version, payload, occurred_at, orders_authorized)
                       VALUES (%s, %s, NULL, %s, %s, %s, NULL, '{}'::jsonb, %s, false)""",
                    (event_id, record.opportunity_id, record.state,
                     "Opportunity discovered", "opportunity_store", stamp),
                )
                return self._opportunity(inserted)

            existing = conn.execute(
                """SELECT * FROM paper_ledger.opportunities
                   WHERE idempotency_key = %s""", (key,)
            ).fetchone()
            if existing is None:
                existing = conn.execute(
                    """SELECT * FROM paper_ledger.opportunities
                       WHERE market_type = %s AND symbol = %s
                         AND strategy_id = %s AND setup_fingerprint = %s""",
                    (record.market_type, record.symbol, record.strategy_id, record.setup_fingerprint),
                ).fetchone()
            if existing is None:
                raise RuntimeError("opportunity insert conflicted but no matching record exists")
            identity = (
                existing["market_type"], existing["symbol"], existing["strategy_id"],
                existing["setup_fingerprint"],
            )
            requested = (record.market_type, record.symbol, record.strategy_id, record.setup_fingerprint)
            if identity != requested:
                raise ValueError("idempotency_key was already used for a different opportunity")
            return self._opportunity(existing)

    def get_opportunity(self, opportunity_id: str) -> OpportunityRecord | None:
        _required_text(opportunity_id, "opportunity_id", 160)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM paper_ledger.opportunities WHERE opportunity_id = %s",
                (opportunity_id,),
            ).fetchone()
        return self._opportunity(row) if row else None

    def list_opportunities(
        self, *, states: Iterable[str] | None = None, symbol: str | None = None,
        limit: int = 50, cursor: str | None = None,
    ) -> OpportunityPage:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        state_values = tuple(states or ())
        if any(state not in _STATES for state in state_values):
            raise ValueError("states contains an invalid opportunity state")
        if symbol is not None:
            symbol = _required_text(symbol, "symbol", 30).upper()
        cursor_row = None
        with self._connect() as conn:
            if cursor is not None:
                cursor = _required_text(cursor, "cursor", 160)
                cursor_row = conn.execute(
                    "SELECT created_at, opportunity_id FROM paper_ledger.opportunities WHERE opportunity_id = %s",
                    (cursor,),
                ).fetchone()
                if cursor_row is None:
                    raise ValueError("cursor opportunity does not exist")
            clauses = ["true"]
            params: list[Any] = []
            if state_values:
                clauses.append("state = ANY(%s)")
                params.append(list(state_values))
            if symbol:
                clauses.append("symbol = %s")
                params.append(symbol)
            if cursor_row:
                clauses.append("(created_at, opportunity_id) < (%s, %s)")
                params.extend((cursor_row["created_at"], cursor_row["opportunity_id"]))
            params.append(limit + 1)
            rows = conn.execute(
                f"""SELECT * FROM paper_ledger.opportunities
                    WHERE {' AND '.join(clauses)}
                    ORDER BY created_at DESC, opportunity_id DESC LIMIT %s""",
                params,
            ).fetchall()
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        return OpportunityPage(
            items=tuple(self._opportunity(row) for row in page_rows),
            next_cursor=page_rows[-1]["opportunity_id"] if has_more and page_rows else None,
        )

    def list_events(self, opportunity_id: str) -> tuple[OpportunityEventRecord, ...]:
        _required_text(opportunity_id, "opportunity_id", 160)
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM paper_ledger.opportunity_events
                   WHERE opportunity_id = %s ORDER BY opportunity_version""",
                (opportunity_id,),
            ).fetchall()
        return tuple(self._event(row) for row in rows)

    def append_assessment(
        self, record: StrategyAssessmentRecord, *, idempotency_key: str,
        now: datetime | None = None,
    ) -> StrategyAssessmentRecord:
        if not isinstance(record, StrategyAssessmentRecord) or record.orders_authorized is not False:
            raise ValueError("assessment must deny order authorization")
        key, stamp = self._idempotency(idempotency_key), _utc(now)
        payload, refs = _json(record.payload, "assessment payload"), _json(record.evidence_refs, "evidence_refs")
        with self._connect() as conn:
            row = conn.execute(
                """INSERT INTO paper_ledger.opportunity_assessments
                   (assessment_id, opportunity_id, idempotency_key, strategy_id,
                    strategy_version, assessment_version, decision, payload,
                    evidence_refs, orders_authorized, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, false, %s)
                   ON CONFLICT DO NOTHING RETURNING *""",
                (record.assessment_id, record.opportunity_id, key, record.strategy_id,
                 record.strategy_version, record.assessment_version, record.decision,
                 payload, refs, stamp),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM paper_ledger.opportunity_assessments WHERE idempotency_key = %s",
                    (key,),
                ).fetchone()
                if row is None or row["assessment_id"] != record.assessment_id:
                    raise ValueError("assessment idempotency key conflicts with another record")
            return StrategyAssessmentRecord(
                assessment_id=row["assessment_id"], opportunity_id=row["opportunity_id"],
                strategy_id=row["strategy_id"], strategy_version=row["strategy_version"],
                assessment_version=row["assessment_version"], decision=row["decision"],
                payload=row["payload"], evidence_refs=row["evidence_refs"],
                orders_authorized=row["orders_authorized"],
            )

    def append_evidence(
        self, record: OpportunityEvidenceRecord, *, idempotency_key: str,
        now: datetime | None = None,
    ) -> OpportunityEvidenceRecord:
        if not isinstance(record, OpportunityEvidenceRecord) or record.orders_authorized is not False:
            raise ValueError("evidence must deny order authorization")
        key, stamp = self._idempotency(idempotency_key), _utc(now)
        payload = _json(record.payload, "evidence payload")
        with self._connect() as conn:
            row = conn.execute(
                """INSERT INTO paper_ledger.opportunity_evidence
                   (evidence_id, opportunity_id, idempotency_key, evidence_type,
                    reference_id, observed_at, payload, orders_authorized, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, false, %s)
                   ON CONFLICT DO NOTHING RETURNING *""",
                (record.evidence_id, record.opportunity_id, key, record.evidence_type,
                 record.reference_id, _utc(record.observed_at, field_name="observed_at"),
                 payload, stamp),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM paper_ledger.opportunity_evidence WHERE idempotency_key = %s",
                    (key,),
                ).fetchone()
                if row is None or row["evidence_id"] != record.evidence_id:
                    raise ValueError("evidence idempotency key conflicts with another record")
            return OpportunityEvidenceRecord(
                evidence_id=row["evidence_id"], opportunity_id=row["opportunity_id"],
                evidence_type=row["evidence_type"], reference_id=row["reference_id"],
                observed_at=row["observed_at"], payload=row["payload"],
                orders_authorized=row["orders_authorized"],
            )

    def save_watch_condition(
        self, record: WatchConditionRecord, *, idempotency_key: str,
        now: datetime | None = None,
    ) -> WatchConditionRecord:
        if not isinstance(record, WatchConditionRecord) or record.orders_authorized is not False:
            raise ValueError("watch condition must deny order authorization")
        key = self._idempotency(idempotency_key)
        stamp = _utc(now)
        payload = _json(record.payload, "watch payload")
        with self._connect() as conn:
            row = conn.execute(
                """INSERT INTO paper_ledger.opportunity_watch_conditions
                   (watch_id, opportunity_id, idempotency_key, trigger_rule,
                    trigger_price, invalidation_price, timeframe, expires_at,
                    payload, orders_authorized, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, false, %s)
                   ON CONFLICT DO NOTHING RETURNING *""",
                (record.watch_id, record.opportunity_id, key, record.trigger_rule,
                 record.trigger_price, record.invalidation_price, record.timeframe,
                 _utc(record.expires_at, field_name="expires_at"), payload, stamp),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    """SELECT * FROM paper_ledger.opportunity_watch_conditions
                       WHERE opportunity_id = %s""", (record.opportunity_id,)
                ).fetchone()
                if row is None or row["watch_id"] != record.watch_id:
                    raise ValueError("watch condition idempotency key conflicts with another record")
            return self._watch(row)

    @staticmethod
    def _watch(row: dict[str, Any]) -> WatchConditionRecord:
        return WatchConditionRecord(
            watch_id=row["watch_id"], opportunity_id=row["opportunity_id"],
            trigger_rule=row["trigger_rule"], trigger_price=row["trigger_price"],
            invalidation_price=row["invalidation_price"], timeframe=row["timeframe"],
            expires_at=row["expires_at"], payload=row["payload"],
            orders_authorized=row["orders_authorized"],
        )

    def get_watch_condition(self, opportunity_id: str) -> WatchConditionRecord | None:
        _required_text(opportunity_id, "opportunity_id", 160)
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM paper_ledger.opportunity_watch_conditions
                   WHERE opportunity_id = %s""", (opportunity_id,)
            ).fetchone()
        return self._watch(row) if row else None

    def transition(
        self, opportunity_id: str, *, expected_version: int, to_state: str,
        reason: str, event_id: str, actor: str, evidence_version: str | None,
        now: datetime | None = None,
    ) -> TransitionResult:
        _required_text(opportunity_id, "opportunity_id", 160)
        _required_text(event_id, "event_id", 200)
        _required_text(reason, "reason", 1000)
        _required_text(actor, "actor", 120)
        if to_state not in _STATES:
            raise ValueError("target opportunity state is invalid")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
            raise ValueError("expected_version must be a non-negative integer")
        if evidence_version is not None:
            _required_text(evidence_version, "evidence_version", 160)
        stamp = _utc(now)
        with self._connect() as conn:
            duplicate = conn.execute(
                "SELECT * FROM paper_ledger.opportunity_events WHERE event_id = %s",
                (event_id,),
            ).fetchone()
            if duplicate is not None:
                if duplicate["opportunity_id"] != opportunity_id or duplicate["to_state"] != to_state:
                    raise ValueError("event_id was already used for a different transition")
                current = conn.execute(
                    "SELECT * FROM paper_ledger.opportunities WHERE opportunity_id = %s",
                    (opportunity_id,),
                ).fetchone()
                if current is None:
                    raise RuntimeError("opportunity disappeared after a recorded event")
                return TransitionResult(self._opportunity(current), self._event(duplicate), True)

            current = conn.execute(
                "SELECT * FROM paper_ledger.opportunities WHERE opportunity_id = %s FOR UPDATE",
                (opportunity_id,),
            ).fetchone()
            if current is None:
                raise ValueError("opportunity does not exist")
            if current["version"] != expected_version:
                raise RuntimeError("stale opportunity version conflict")
            from_state = current["state"]
            if from_state in _TERMINAL_STATES or to_state not in _ALLOWED_TRANSITIONS.get(from_state, set()):
                raise ValueError(f"invalid opportunity transition: {from_state} -> {to_state}")
            if to_state == "INTENT_RECORDED" and (
                from_state != "RISK_CHECK" or actor != "risk_manager"
            ):
                raise ValueError("intent recording requires Risk Manager approval")
            if to_state == "PAPER_OPEN" and (
                from_state != "INTENT_RECORDED" or actor != "paper_ledger" or evidence_version is None
            ):
                raise ValueError("PAPER_OPEN requires ledger-confirmed position evidence")
            updated = conn.execute(
                """UPDATE paper_ledger.opportunities
                   SET state = %s, version = version + 1, updated_at = %s
                   WHERE opportunity_id = %s AND version = %s
                   RETURNING *""",
                (to_state, stamp, opportunity_id, expected_version),
            ).fetchone()
            if updated is None:
                raise RuntimeError("stale opportunity version conflict")
            event_row = conn.execute(
                """INSERT INTO paper_ledger.opportunity_events
                   (event_id, opportunity_id, from_state, to_state, reason, actor,
                    evidence_version, payload, occurred_at, orders_authorized)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, '{}'::jsonb, %s, false)
                   RETURNING *""",
                (event_id, opportunity_id, from_state, to_state, reason, actor,
                 evidence_version, stamp),
            ).fetchone()
            return TransitionResult(self._opportunity(updated), self._event(event_row))

    def claim_due_opportunities(
        self, *, owner: str, now: datetime | None = None,
        limit: int = 20, lease_seconds: float = 60.0,
    ) -> tuple[OpportunityRecord, ...]:
        owner = _required_text(owner, "owner", 120)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if not math.isfinite(lease_seconds) or lease_seconds < 5:
            raise ValueError("lease_seconds must be finite and at least 5 seconds")
        stamp = _utc(now)
        expiry = stamp + timedelta(seconds=float(lease_seconds))
        claimed: list[OpportunityRecord] = []
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT o.* FROM paper_ledger.opportunities o
                   LEFT JOIN paper_ledger.opportunity_claims c
                     ON c.opportunity_id = o.opportunity_id
                   WHERE o.state IN ('WAITING', 'REASSESSING')
                     AND (o.expires_at IS NULL OR o.expires_at > %s)
                     AND (c.opportunity_id IS NULL OR c.lease_expires_at <= %s)
                   ORDER BY o.updated_at, o.opportunity_id
                   FOR UPDATE OF o SKIP LOCKED LIMIT %s""",
                (stamp, stamp, limit),
            ).fetchall()
            for row in rows:
                lease = conn.execute(
                    """INSERT INTO paper_ledger.opportunity_claims
                       (opportunity_id, lease_owner, claimed_at, lease_expires_at, orders_authorized)
                       VALUES (%s, %s, %s, %s, false)
                       ON CONFLICT (opportunity_id) DO UPDATE SET
                         lease_owner = EXCLUDED.lease_owner,
                         claimed_at = EXCLUDED.claimed_at,
                         lease_expires_at = EXCLUDED.lease_expires_at
                       WHERE paper_ledger.opportunity_claims.lease_expires_at <= %s
                       RETURNING opportunity_id""",
                    (row["opportunity_id"], owner, stamp, expiry, stamp),
                ).fetchone()
                if lease is not None:
                    claimed.append(self._opportunity(row))
        return tuple(claimed)

    def release_claim(self, opportunity_id: str, *, owner: str, now: datetime | None = None) -> bool:
        _required_text(opportunity_id, "opportunity_id", 160)
        owner = _required_text(owner, "owner", 120)
        _utc(now)
        with self._connect() as conn:
            row = conn.execute(
                """DELETE FROM paper_ledger.opportunity_claims
                   WHERE opportunity_id = %s AND lease_owner = %s
                   RETURNING opportunity_id""",
                (opportunity_id, owner),
            ).fetchone()
        return row is not None
