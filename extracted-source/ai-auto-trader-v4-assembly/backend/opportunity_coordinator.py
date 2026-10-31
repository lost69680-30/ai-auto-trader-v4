"""Durable multi-opportunity lifecycle coordination; triggers never create orders."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from typing import Any, Callable, Iterable

from app.engines.price_planner import PricePlanResult
from app.engines.signal_handoff import SignalPricePlan
from backend.opportunity_store import (
    OpportunityRecord,
    OpportunityEvidenceRecord,
    PostgresOpportunityStore,
    StrategyAssessmentRecord,
    WatchConditionRecord,
)
from backend.signal_watcher import MarketTick, SignalWatcher, WatchRequest, WatcherConfig


@dataclass(frozen=True, slots=True)
class OpportunityEvaluation:
    opportunity_id: str
    status: str
    state: str
    reason: str
    observed_price: float | None
    orders_authorized: bool = False


@dataclass(frozen=True, slots=True)
class CoordinatorRunResult:
    results: tuple[OpportunityEvaluation, ...]
    evaluated_count: int
    failed_count: int
    orders_authorized: bool = False


MarketUpdateProvider = Callable[[str, datetime], MarketTick]


def _utc(value: datetime, name: str = "timestamp") -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _required_text(value: str, name: str, maximum: int = 240) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a trimmed string of 1 to {maximum} characters")
    return value


def _paper_safe(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "orders_authorized" and item is not False:
                return False
            if not _paper_safe(item):
                return False
    elif isinstance(value, (list, tuple)):
        return all(_paper_safe(item) for item in value)
    return True


def _fingerprint(
    *, symbol: str, market_type: str, venue: str, strategy_id: str,
    strategy_version: str, direction: str, timeframe: str, plan: SignalPricePlan,
) -> str:
    identity = {
        "symbol": symbol,
        "market_type": market_type,
        "venue": venue,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "direction": direction,
        "timeframe": timeframe,
        "trigger_rule": plan.trigger_rule,
        "trigger_price": plan.trigger_price,
        "invalidation_price": plan.invalidation_price,
        "entry_price": plan.entry_price,
        "stop_price": plan.stop_price,
        "target_price": plan.target_price,
        "estimated_round_trip_cost_pct": plan.estimated_round_trip_cost_pct,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _evidence_version(refs: Iterable[str]) -> str:
    encoded = "|".join(sorted(set(refs)))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def _price_plan_payload(plan: SignalPricePlan) -> dict[str, Any]:
    return {
        "signal_id": plan.signal_id,
        "direction": plan.direction,
        "created_at": _utc(plan.created_at, "price plan created_at").isoformat(),
        "expires_at": _utc(plan.expires_at, "price plan expires_at").isoformat(),
        "market_data_at": _utc(plan.market_data_at, "price plan market_data_at").isoformat(),
        "entry_price": plan.entry_price,
        "stop_price": plan.stop_price,
        "target_price": plan.target_price,
        "estimated_round_trip_cost_pct": plan.estimated_round_trip_cost_pct,
        "trigger_price": plan.trigger_price,
        "trigger_rule": plan.trigger_rule,
        "invalidation_price": plan.invalidation_price,
        "orders_authorized": False,
    }


class OpportunityCoordinator:
    """Create durable waits from explicit plans and evaluate every matching watch."""

    def __init__(
        self,
        store: PostgresOpportunityStore,
        *,
        owner: str,
        watcher_config: WatcherConfig | None = None,
        lease_seconds: float = 60.0,
        max_active_opportunities_per_symbol: int = 100,
    ) -> None:
        if not isinstance(store, PostgresOpportunityStore):
            raise ValueError("a PostgreSQL opportunity store is required")
        self.store = store
        self.owner = _required_text(owner, "owner", 120)
        if not math.isfinite(lease_seconds) or lease_seconds < 5:
            raise ValueError("lease_seconds must be finite and at least 5 seconds")
        if (
            isinstance(max_active_opportunities_per_symbol, bool)
            or not isinstance(max_active_opportunities_per_symbol, int)
            or not 1 <= max_active_opportunities_per_symbol <= 100
        ):
            raise ValueError("max_active_opportunities_per_symbol must be between 1 and 100")
        self.lease_seconds = float(lease_seconds)
        self.max_active_opportunities_per_symbol = max_active_opportunities_per_symbol
        self.watcher_config = watcher_config or WatcherConfig()

    def register_assessed_opportunity(
        self,
        assessment: StrategyAssessmentRecord,
        *,
        price_plan_result: PricePlanResult,
        symbol: str,
        market_type: str,
        venue: str,
        timeframe: str,
        evidence_refs: Iterable[str],
        now: datetime,
    ) -> OpportunityRecord:
        """Persist a strategy-approved setup only when an explicit price plan is READY."""
        stamp = _utc(now, "now")
        if not isinstance(assessment, StrategyAssessmentRecord) or assessment.orders_authorized is not False:
            raise ValueError("a paper-only strategy assessment is required")
        if not _paper_safe(assessment.payload):
            raise ValueError("strategy assessment payload violates the paper-only boundary")
        if assessment.strategy_id == "long_term" or assessment.payload.get("analysis_only") is True:
            raise ValueError("long-term analysis is analysis-only and cannot enter the waiting engine")
        if (
            assessment.decision != "ELIGIBLE"
            or assessment.payload.get("eligible_for_risk_review") is not True
            or assessment.payload.get("paper_eligible") is not True
        ):
            raise ValueError("only an independently eligible executable strategy assessment can be watched")
        direction = assessment.payload.get("direction")
        if direction not in {"LONG", "SHORT"}:
            raise ValueError("eligible strategy assessment must have a LONG or SHORT direction")
        symbol = _required_text(symbol, "symbol", 30)
        if symbol != symbol.upper():
            raise ValueError("symbol must be uppercase")
        if market_type != "FUTURES" or venue != "BINANCE_USDM_TESTNET":
            raise ValueError("opportunity waiting is currently restricted to Binance USD-M Futures Testnet")
        timeframe = _required_text(timeframe, "timeframe", 12)
        required_timeframes = assessment.payload.get("required_timeframes")
        if not isinstance(required_timeframes, list) or timeframe not in required_timeframes:
            raise ValueError("watch timeframe must be one of the strategy's required timeframes")
        if not isinstance(price_plan_result, PricePlanResult) or price_plan_result.orders_authorized is not False:
            raise ValueError("a paper-only PricePlanResult is required")
        if price_plan_result.status != "READY" or not isinstance(price_plan_result.plan, SignalPricePlan):
            raise ValueError("price plan must have READY status and an explicit SignalPricePlan")
        plan = price_plan_result.plan
        if plan.direction != direction:
            raise ValueError("price plan direction does not match strategy assessment")
        _required_text(plan.signal_id, "signal_id", 160)
        created_at = _utc(plan.created_at, "price plan created_at")
        expires_at = _utc(plan.expires_at, "price plan expires_at")
        market_data_at = _utc(plan.market_data_at, "price plan market_data_at")
        if expires_at <= stamp:
            raise ValueError("price plan has expired")
        if created_at > stamp + timedelta(seconds=2) or market_data_at > stamp + timedelta(seconds=2):
            raise ValueError("price plan contains a future timestamp")
        if (stamp - market_data_at).total_seconds() > 30:
            raise ValueError("price plan market reference is stale")
        numeric = (
            plan.entry_price, plan.stop_price, plan.target_price,
            plan.trigger_price, plan.estimated_round_trip_cost_pct,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            for value in numeric
        ):
            raise ValueError("price plan values must be finite numbers")
        if min(plan.entry_price, plan.stop_price, plan.target_price, plan.trigger_price) <= 0:
            raise ValueError("price plan prices must be positive")
        if plan.estimated_round_trip_cost_pct < 0 or plan.invalidation_price is None:
            raise ValueError("price plan requires non-negative costs and an invalidation price")
        # Reuse WatchRequest's direction-specific trigger/invalidation validation.
        fingerprint = _fingerprint(
            symbol=symbol, market_type=market_type, venue=venue,
            strategy_id=assessment.strategy_id, strategy_version=assessment.strategy_version,
            direction=direction, timeframe=timeframe, plan=plan,
        )
        opportunity_id = "opp-" + fingerprint[:40]
        watch_id = "watch-" + fingerprint[:40]
        WatchRequest(
            watch_id=watch_id,
            signal_id=plan.signal_id,
            symbol=symbol,
            direction=direction,
            trigger_price=plan.trigger_price,
            trigger_rule=plan.trigger_rule,
            created_at=created_at,
            expires_at=expires_at,
            invalidation_price=plan.invalidation_price,
        )

        refs = tuple(dict.fromkeys(
            _required_text(ref, "evidence_ref", 240)
            for ref in (*tuple(assessment.evidence_refs), *tuple(evidence_refs))
        ))
        if not refs:
            raise ValueError("opportunity requires at least one evidence reference")
        evidence_version = _evidence_version(refs)
        metadata = {
            "assessment_id": assessment.assessment_id,
            "assessment_version": assessment.assessment_version,
            "required_timeframes": list(required_timeframes),
            "evidence_refs": list(refs),
            "price_plan": _price_plan_payload(plan),
            "paper_eligible": True,
            "analysis_only": False,
            "orders_authorized": False,
        }
        record = OpportunityRecord(
            opportunity_id=opportunity_id,
            symbol=symbol,
            market_type=market_type,
            venue=venue,
            strategy_id=assessment.strategy_id,
            strategy_version=assessment.strategy_version,
            setup_fingerprint=fingerprint,
            direction=direction,
            timeframe=timeframe,
            state="DISCOVERED",
            version=0,
            expires_at=expires_at,
            metadata=metadata,
            orders_authorized=False,
        )
        saved = self.store.create_or_get_opportunity(
            record, idempotency_key=f"opportunity:{fingerprint}", now=stamp,
        )
        if saved.state in {"TRIGGERED", "RISK_CHECK", "INTENT_RECORDED", "PAPER_OPEN",
                           "REJECTED", "INVALIDATED", "EXPIRED", "CANCELLED", "BLOCKED", "NO_TRADE"}:
            return saved

        persisted_assessment_id = "assessment-" + hashlib.sha256(
            f"{opportunity_id}|{assessment.strategy_id}|{assessment.assessment_version}".encode("utf-8")
        ).hexdigest()[:40]
        persisted_assessment = StrategyAssessmentRecord(
            assessment_id=persisted_assessment_id,
            opportunity_id=opportunity_id,
            strategy_id=assessment.strategy_id,
            strategy_version=assessment.strategy_version,
            assessment_version=assessment.assessment_version,
            decision=assessment.decision,
            payload=dict(assessment.payload),
            evidence_refs=list(refs),
            orders_authorized=False,
        )
        self.store.append_assessment(
            persisted_assessment,
            idempotency_key=f"assessment:{opportunity_id}:{assessment.strategy_id}:{assessment.assessment_version}",
            now=stamp,
        )

        if saved.state == "DISCOVERED":
            saved = self.store.transition(
                opportunity_id, expected_version=saved.version, to_state="ASSESSING",
                reason="independent strategy assessment and explicit price plan accepted",
                event_id="event-assessing-" + fingerprint[:40],
                actor="opportunity_coordinator", evidence_version=evidence_version, now=stamp,
            ).opportunity

        watch = WatchConditionRecord(
            watch_id=watch_id,
            opportunity_id=opportunity_id,
            trigger_rule=plan.trigger_rule,
            trigger_price=plan.trigger_price,
            invalidation_price=plan.invalidation_price,
            timeframe=timeframe,
            expires_at=expires_at,
            payload={
                "signal_id": plan.signal_id,
                "entry_price": plan.entry_price,
                "stop_price": plan.stop_price,
                "target_price": plan.target_price,
                "orders_authorized": False,
            },
            orders_authorized=False,
        )
        self.store.save_watch_condition(
            watch, idempotency_key=f"watch:{fingerprint}", now=stamp,
        )
        if saved.state == "ASSESSING":
            saved = self.store.transition(
                opportunity_id, expected_version=saved.version, to_state="WAITING",
                reason="durable watch condition stored; waiting for fresh market confirmation",
                event_id="event-waiting-" + fingerprint[:40],
                actor="opportunity_coordinator", evidence_version=evidence_version, now=stamp,
            ).opportunity
        return self.store.get_opportunity(opportunity_id) or saved

    def evaluate_market_update(
        self, symbol: str, tick: MarketTick, *, now: datetime,
    ) -> tuple[OpportunityEvaluation, ...]:
        symbol = _required_text(symbol, "symbol", 30)
        if symbol != symbol.upper() or not isinstance(tick, MarketTick) or tick.symbol != symbol:
            raise ValueError("market update symbol must match the normalized requested symbol")
        page = self.store.list_opportunities(
            states=("WAITING", "REASSESSING"),
            symbol=symbol,
            limit=self.max_active_opportunities_per_symbol,
        )
        if page.next_cursor is not None:
            raise RuntimeError("active opportunity limit exceeded; refusing partial watch evaluation")
        return self._evaluate_records(page.items, tick, now=_utc(now, "now"))

    def _evaluate_records(
        self, records: Iterable[OpportunityRecord], tick: MarketTick, *, now: datetime,
    ) -> tuple[OpportunityEvaluation, ...]:
        rows = tuple(records)
        if not rows:
            return ()
        watcher = SignalWatcher(self.watcher_config)
        by_watch: dict[str, OpportunityRecord] = {}
        blocked: list[OpportunityEvaluation] = []
        for record in rows:
            watch = self.store.get_watch_condition(record.opportunity_id)
            plan = record.metadata.get("price_plan") if isinstance(record.metadata, dict) else None
            signal_id = plan.get("signal_id") if isinstance(plan, dict) else None
            if watch is None or not isinstance(signal_id, str) or not signal_id.strip():
                try:
                    transitioned = self.store.transition(
                        record.opportunity_id, expected_version=record.version,
                        to_state="BLOCKED", reason="active opportunity is missing its durable watch or signal identity",
                        event_id=self._event_id(record, "BLOCKED", tick.observed_at),
                        actor="opportunity_coordinator",
                        evidence_version=tick.observed_at.astimezone(timezone.utc).isoformat(),
                        now=now,
                    )
                    blocked.append(OpportunityEvaluation(
                        record.opportunity_id, "BLOCKED", transitioned.opportunity.state,
                        "active opportunity is missing its durable watch or signal identity",
                        tick.price, False,
                    ))
                except (ValueError, RuntimeError):
                    latest = self.store.get_opportunity(record.opportunity_id)
                    blocked.append(OpportunityEvaluation(
                        record.opportunity_id, "BLOCKED", latest.state if latest else "UNKNOWN",
                        "unable to block invalid opportunity due to a state conflict",
                        tick.price, False,
                    ))
                continue
            try:
                request = WatchRequest(
                    watch_id=watch.watch_id,
                    signal_id=signal_id,
                    symbol=record.symbol,
                    direction=record.direction,
                    trigger_price=watch.trigger_price,
                    trigger_rule=watch.trigger_rule,
                    created_at=_utc(record.metadata["price_plan"]["created_at"] and datetime.fromisoformat(
                        record.metadata["price_plan"]["created_at"]
                    ), "price plan created_at"),
                    expires_at=watch.expires_at,
                    invalidation_price=watch.invalidation_price,
                )
                watcher.add(request)
                by_watch[watch.watch_id] = record
            except (KeyError, TypeError, ValueError):
                try:
                    transitioned = self.store.transition(
                        record.opportunity_id, expected_version=record.version,
                        to_state="BLOCKED", reason="durable watch condition failed integrity validation",
                        event_id=self._event_id(record, "BLOCKED", tick.observed_at),
                        actor="opportunity_coordinator",
                        evidence_version=tick.observed_at.astimezone(timezone.utc).isoformat(),
                        now=now,
                    )
                    blocked.append(OpportunityEvaluation(
                        record.opportunity_id, "BLOCKED", transitioned.opportunity.state,
                        "durable watch condition failed integrity validation", tick.price, False,
                    ))
                except (ValueError, RuntimeError):
                    latest = self.store.get_opportunity(record.opportunity_id)
                    blocked.append(OpportunityEvaluation(
                        record.opportunity_id, "BLOCKED", latest.state if latest else "UNKNOWN",
                        "unable to block invalid watch after a state conflict", tick.price, False,
                    ))

        if not by_watch:
            return tuple(blocked)
        watch_results = watcher.evaluate_all(tick, now=now)
        evaluations = list(blocked)
        for result in watch_results:
            record = by_watch[result.watch_id]
            target_state = result.status
            if result.status == "WAITING" and record.state == "REASSESSING":
                target_state = "WAITING"
            if target_state in {"TRIGGERED", "INVALIDATED", "EXPIRED"} or (
                target_state == "WAITING" and record.state == "REASSESSING"
            ):
                try:
                    transitioned = self.store.transition(
                        record.opportunity_id,
                        expected_version=record.version,
                        to_state=target_state,
                        reason=result.reason,
                        event_id=self._event_id(record, target_state, tick.observed_at),
                        actor="opportunity_coordinator",
                        evidence_version=tick.observed_at.astimezone(timezone.utc).isoformat(),
                        now=now,
                    )
                    state = transitioned.opportunity.state
                except (ValueError, RuntimeError):
                    latest = self.store.get_opportunity(record.opportunity_id)
                    state = latest.state if latest else "UNKNOWN"
                    evaluations.append(OpportunityEvaluation(
                        record.opportunity_id, "STATE_CONFLICT", state,
                        "opportunity changed concurrently; this market update did not overwrite it",
                        result.observed_price, False,
                    ))
                    continue
            else:
                state = record.state
            evaluations.append(OpportunityEvaluation(
                record.opportunity_id, result.status, state, result.reason,
                result.observed_price, False,
            ))
        return tuple(evaluations)

    def reassess_due(
        self,
        *,
        now: datetime,
        market_update_provider: MarketUpdateProvider,
        limit: int = 20,
    ) -> CoordinatorRunResult:
        stamp = _utc(now, "now")
        if not callable(market_update_provider):
            raise ValueError("market_update_provider must be callable")
        claimed = self.store.claim_due_opportunities(
            owner=self.owner, now=stamp, limit=limit, lease_seconds=self.lease_seconds,
        )
        grouped: dict[str, list[OpportunityRecord]] = {}
        for record in claimed:
            grouped.setdefault(record.symbol, []).append(record)

        results: list[OpportunityEvaluation] = []
        for symbol, records in grouped.items():
            try:
                tick = market_update_provider(symbol, stamp)
                if not isinstance(tick, MarketTick) or tick.symbol != symbol:
                    raise ValueError("market update provider returned a mismatched symbol")
                results.extend(self._evaluate_records(records, tick, now=stamp))
            except Exception as exc:
                for record in records:
                    results.append(OpportunityEvaluation(
                        record.opportunity_id, "WAIT_RECHECK", record.state,
                        f"fresh market update unavailable ({type(exc).__name__})",
                        None, False,
                    ))
            finally:
                for record in records:
                    self.store.release_claim(record.opportunity_id, owner=self.owner, now=stamp)
        return CoordinatorRunResult(
            results=tuple(results),
            evaluated_count=len(results),
            failed_count=sum(item.status in {"WAIT_RECHECK", "STATE_CONFLICT", "BLOCKED"} for item in results),
            orders_authorized=False,
        )

    def cancel(self, opportunity_id: str, *, reason: str, now: datetime) -> OpportunityRecord:
        record = self.store.get_opportunity(opportunity_id)
        if record is None:
            raise ValueError("opportunity does not exist")
        event_id = self._event_id(record, "CANCELLED", _utc(now, "now"))
        return self.store.transition(
            opportunity_id,
            expected_version=record.version,
            to_state="CANCELLED",
            reason=reason,
            event_id=event_id,
            actor="opportunity_coordinator",
            evidence_version=None,
            now=now,
        ).opportunity

    def triggered_opportunity(self, opportunity_id: str) -> OpportunityRecord | None:
        record = self.store.get_opportunity(opportunity_id)
        return record if record is not None and record.state == "TRIGGERED" else None

    @staticmethod
    def _event_id(record: OpportunityRecord, state: str, observed_at: datetime) -> str:
        token = f"{record.opportunity_id}|{record.version}|{state}|{_utc(observed_at).isoformat()}"
        return "event-" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:48]
