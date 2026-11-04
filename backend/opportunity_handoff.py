"""Fresh trigger revalidation and paper-only handoff for durable opportunities.

This adapter has no exchange-order capability. Futures opportunities can pass
through the existing signal handoff, PaperSignalCycle, Risk Guardian and paper
ledger. Spot opportunities remain research-only until a Spot paper runtime exists.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import math
from typing import Any

from app.engines.price_planner import PricePlanRequest, PricePlanningConfig, plan_prices
from app.engines.research_pipeline import ResearchCandidate
from app.engines.signal_handoff import prepare_signal
from backend.indicator_intelligence import assess_indicators
from backend.market_assessment import assess_market
from backend.market_data_adapter import MarketCandle
from backend.market_intelligence_scan import (
    OPPORTUNITY_TIMEFRAME_MAX_AGE_SECONDS,
    SCAN_INTERVALS,
    _yearly_candles,
)
from backend.opportunity_coordinator import OpportunityCoordinator
from backend.opportunity_store import OpportunityEvidenceRecord, OpportunityRecord
from backend.signal_watcher import MarketTick
from backend.strategy_assessment_engine import MultiStrategyAssessmentEngine, StrategyCandidate
from backend.strategy_research import Candle as StrategyCandle, ResearchConfig, research_strategies


def _signal_id(opportunity_id: str) -> str:
    digest = hashlib.sha256(opportunity_id.encode("utf-8")).hexdigest()[:40]
    return f"opportunity-{digest}"


def _evidence_version(refs: list[str], assessments: tuple[Any, ...]) -> str:
    material = "|".join(sorted(refs) + sorted(item.assessment_id for item in assessments))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def _append_revalidation_evidence(
    coordinator: OpportunityCoordinator, record: OpportunityRecord, *,
    refs: list[str], assessments: tuple[Any, ...], now: datetime,
    market_type: str, mark_price: float, mark_observed_at: datetime,
) -> str:
    version = max(1, record.version + 1)
    for assessment in assessments:
        coordinator.store.append_assessment(
            assessment,
            idempotency_key=(
                f"trigger-revalidation:{record.opportunity_id}:"
                f"{assessment.strategy_id}:{assessment.assessment_version}"
            ),
            now=now,
        )
    evidence_version = _evidence_version(refs, assessments)
    evidence_id = "evidence-trigger-" + hashlib.sha256(
        f"{record.opportunity_id}|{evidence_version}".encode("utf-8")
    ).hexdigest()[:40]
    coordinator.store.append_evidence(
        OpportunityEvidenceRecord(
            evidence_id=evidence_id,
            opportunity_id=record.opportunity_id,
            evidence_type="TRIGGER_REVALIDATION",
            reference_id=f"trigger-revalidation:{record.opportunity_id}:{version}",
            observed_at=mark_observed_at,
            payload={
                "market_type": market_type,
                "mark_price": mark_price,
                "mark_observed_at": mark_observed_at.astimezone(timezone.utc).isoformat(),
                "strategy_assessment_ids": [item.assessment_id for item in assessments],
                "strategy_decisions": {
                    item.strategy_id: {
                        "decision": item.decision,
                        "direction": item.payload.get("direction"),
                        "eligible_for_risk_review": item.payload.get("eligible_for_risk_review") is True,
                        "reasons": item.payload.get("reasons", []),
                        "orders_authorized": False,
                    }
                    for item in assessments
                },
                "evidence_refs": refs,
                "orders_authorized": False,
            },
            orders_authorized=False,
        ),
        idempotency_key=f"trigger-revalidation:{record.opportunity_id}:{evidence_version}",
        now=now,
    )
    return evidence_version


def _transition_blocked(
    coordinator: OpportunityCoordinator, opportunity_id: str, *,
    reason: str, now: datetime, evidence_version: str | None = None,
    target_state: str = "BLOCKED",
) -> OpportunityRecord:
    latest = coordinator.store.get_opportunity(opportunity_id)
    if latest is None:
        raise ValueError("triggered opportunity no longer exists")
    if latest.state not in {"TRIGGERED", "RISK_CHECK"}:
        return latest
    event_id = "event-handoff-" + hashlib.sha256(
        f"{opportunity_id}|{latest.version}|{target_state}|{reason}".encode("utf-8")
    ).hexdigest()[:40]
    return coordinator.store.transition(
        opportunity_id,
        expected_version=latest.version,
        to_state=target_state,
        reason=reason,
        event_id=event_id,
        actor="opportunity_handoff",
        evidence_version=evidence_version,
        now=now,
    ).opportunity


def process_triggered_opportunity(
    coordinator: OpportunityCoordinator,
    record: OpportunityRecord,
    *,
    source: Any,
    paper_service: Any,
    now: datetime,
    strategy_engine: Any | None = None,
) -> dict[str, Any]:
    """Revalidate all five strategies, then hand Futures through the existing paper cycle."""
    if not isinstance(coordinator, OpportunityCoordinator):
        raise ValueError("a durable opportunity coordinator is required")
    if not isinstance(record, OpportunityRecord) or record.orders_authorized is not False:
        raise ValueError("a paper-only durable opportunity is required")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("handoff clock must be timezone-aware")
    stamp = now.astimezone(timezone.utc)
    if record.state not in {"TRIGGERED", "RISK_CHECK"}:
        return {"status": "NOT_READY", "state": record.state, "orders_authorized": False}
    if coordinator.market_type != "FUTURES" or coordinator.venue != "BINANCE_USDM_TESTNET":
        # The current app's Risk Guardian and durable paper runtime are Futures-only.
        # Do not route Spot signals through a Futures account/risk context.
        return {
            "status": "SPOT_RESEARCH_ONLY",
            "state": record.state,
            "reason": "Spot paper handoff is not enabled; no Futures risk context was reused",
            "orders_authorized": False,
        }
    if paper_service is None or not callable(getattr(paper_service, "process_validated_handoff", None)):
        return {
            "status": "HANDOFF_UNAVAILABLE",
            "state": record.state,
            "reason": "existing paper signal cycle is unavailable",
            "orders_authorized": False,
        }
    if record.expires_at is None or record.expires_at <= stamp:
        latest = _transition_blocked(
            coordinator, record.opportunity_id, reason="triggered opportunity expired before handoff",
            now=stamp, target_state="BLOCKED",
        )
        return {"status": "EXPIRED_BEFORE_HANDOFF", "state": latest.state, "orders_authorized": False}

    signal_id = _signal_id(record.opportunity_id)
    proposal_id = f"paper:{signal_id}"
    if record.state == "RISK_CHECK":
        try:
            existing_order = next(
                (item for item in tuple(paper_service.ledger.orders)
                 if item.proposal_id == proposal_id),
                None,
            )
        except Exception:
            return {
                "status": "RECONCILIATION_REQUIRED",
                "state": "RISK_CHECK",
                "reason": "paper ledger state is uncertain; no duplicate handoff was attempted",
                "orders_authorized": False,
            }
        if existing_order is not None:
            coordinator.store.append_evidence(
                OpportunityEvidenceRecord(
                    evidence_id="evidence-paper-intent-" + hashlib.sha256(
                        f"{record.opportunity_id}|{proposal_id}".encode("utf-8")
                    ).hexdigest()[:40],
                    opportunity_id=record.opportunity_id,
                    evidence_type="PAPER_INTENT",
                    reference_id=proposal_id,
                    observed_at=stamp,
                    payload={
                        "proposal_id": proposal_id,
                        "signal_id": signal_id,
                        "ledger_status": existing_order.status,
                        "risk_approved": True,
                        "orders_authorized": False,
                    },
                    orders_authorized=False,
                ),
                idempotency_key=f"paper-intent:{record.opportunity_id}:{proposal_id}",
                now=stamp,
            )
            latest = coordinator.store.get_opportunity(record.opportunity_id)
            if latest is not None and latest.state == "RISK_CHECK":
                latest = coordinator.store.transition(
                    latest.opportunity_id,
                    expected_version=latest.version,
                    to_state="INTENT_RECORDED",
                    reason="recovered Risk Guardian-approved intent from the durable paper ledger",
                    event_id="event-intent-recovered-" + hashlib.sha256(
                        f"{record.opportunity_id}|{proposal_id}".encode("utf-8")
                    ).hexdigest()[:40],
                    actor="risk_manager",
                    evidence_version=proposal_id,
                    now=stamp,
                ).opportunity
            return {
                "status": "INTENT_RECORDED",
                "state": latest.state if latest else "UNKNOWN",
                "proposal_id": proposal_id,
                "orders_authorized": False,
            }

    symbol = record.symbol
    original_plan = record.metadata.get("price_plan", {}) if isinstance(record.metadata, dict) else {}
    watch = coordinator.store.get_watch_condition(record.opportunity_id)
    if not isinstance(original_plan, dict) or watch is None:
        latest = _transition_blocked(
            coordinator, record.opportunity_id, reason="triggered opportunity is missing its stored plan or watch",
            now=stamp,
        )
        return {"status": "BLOCKED", "state": latest.state, "orders_authorized": False}

    try:
        universe = source.fetch_market_universe(now=stamp)
        if getattr(universe, "orders_authorized", None) is not False:
            raise ValueError("market universe authorization flag is invalid")
        universe_time = getattr(universe, "fetched_at", None)
        if not isinstance(universe_time, datetime) or universe_time.tzinfo is None or universe_time.utcoffset() is None:
            raise ValueError("market universe timestamp is invalid")
        universe_age = (stamp - universe_time.astimezone(timezone.utc)).total_seconds()
        if universe_age < -2 or universe_age > 120:
            raise ValueError("market universe is stale or future-dated")

        contracts = [item for item in getattr(universe, "contracts", ()) if item.symbol == symbol]
        snapshots = [item for item in getattr(universe, "snapshots", ()) if item.symbol == symbol]
        if len(contracts) != 1 or len(snapshots) != 1:
            raise ValueError("contract or market snapshot is missing or duplicated")
        contract, market_snapshot = contracts[0], snapshots[0]
        if (
            getattr(contract, "status", None) != "TRADING"
            or not all(
                isinstance(getattr(contract, name, None), (int, float))
                and not isinstance(getattr(contract, name, None), bool)
                and math.isfinite(float(getattr(contract, name)))
                and float(getattr(contract, name)) > 0
                for name in ("tick_size", "quantity_step", "min_quantity")
            )
        ):
            raise ValueError("exchange contract filters are invalid")
        snapshot_at = getattr(market_snapshot, "observed_at", None)
        if not isinstance(snapshot_at, datetime) or snapshot_at.tzinfo is None or snapshot_at.utcoffset() is None:
            raise ValueError("market snapshot timestamp is invalid")
        snapshot_age = (stamp - snapshot_at.astimezone(timezone.utc)).total_seconds()
        if snapshot_age < -2 or snapshot_age > 120:
            raise ValueError("market snapshot is stale or future-dated")
        spread_bps = getattr(market_snapshot, "spread_bps", None)
        quote_volume = getattr(market_snapshot, "quote_volume_24h", None)
        if (
            not isinstance(spread_bps, (int, float)) or isinstance(spread_bps, bool)
            or not math.isfinite(spread_bps) or spread_bps < 0
            or not isinstance(quote_volume, (int, float)) or isinstance(quote_volume, bool)
            or not math.isfinite(quote_volume) or quote_volume < 0
        ):
            raise ValueError("market liquidity evidence is invalid")

        price_snapshot = source.fetch_mark_price(symbol)
        mark_price = getattr(price_snapshot, "mark_price", None)
        mark_at = getattr(price_snapshot, "observed_at", None)
        mark_age = (
            (stamp - mark_at.astimezone(timezone.utc)).total_seconds()
            if isinstance(mark_at, datetime) and mark_at.tzinfo is not None and mark_at.utcoffset() is not None
            else math.inf
        )
        if (
            getattr(price_snapshot, "symbol", None) != symbol
            or getattr(price_snapshot, "market_type", None) != "FUTURES"
            or getattr(price_snapshot, "orders_authorized", None) is not False
            or not isinstance(mark_price, (int, float))
            or isinstance(mark_price, bool) or not math.isfinite(mark_price) or mark_price <= 0
            or mark_age < -2 or mark_age > 30
        ):
            raise ValueError("fresh Futures mark price failed validation")

        # Recheck trigger overshoot after the scheduled job has claimed the trigger.
        entry, trigger = original_plan.get("entry_price"), watch.trigger_price
        if (
            not isinstance(entry, (int, float)) or isinstance(entry, bool)
            or not math.isfinite(entry) or entry <= 0
            or not isinstance(trigger, (int, float)) or isinstance(trigger, bool)
            or not math.isfinite(trigger) or trigger <= 0
        ):
            raise ValueError("stored trigger geometry is invalid")
        allowed_overshoot = abs(float(trigger) - float(entry))
        actual_overshoot = (
            max(0.0, float(mark_price) - float(trigger))
            if record.direction == "LONG"
            else max(0.0, float(trigger) - float(mark_price))
        )
        if actual_overshoot > allowed_overshoot + max(1e-12, allowed_overshoot * 1e-9):
            raise ValueError("fresh mark price exceeded one original ATR trigger buffer")

        candles: dict[str, tuple[MarketCandle, ...]] = {}
        refs = [f"market-snapshot:{symbol}:{snapshot_at.astimezone(timezone.utc).isoformat()}"]
        for interval in SCAN_INTERVALS:
            rows = tuple(source.fetch_klines(symbol, interval=interval, limit=200, now=stamp))
            if any(not isinstance(item, MarketCandle) for item in rows):
                raise ValueError(f"{interval} candle data is malformed")
            closed = tuple(item for item in rows if item.is_closed)
            candles[interval] = closed
            if closed:
                refs.append(
                    f"closed-candle:{symbol}:{interval}:{closed[-1].close_time.astimezone(timezone.utc).isoformat()}"
                )
        candles["1Y"] = _yearly_candles(symbol, candles.get("1M", ()))
        timeframe_age = OPPORTUNITY_TIMEFRAME_MAX_AGE_SECONDS.get(record.timeframe)
        timeframe_rows = candles.get(record.timeframe, ())
        if timeframe_age is None or not timeframe_rows:
            raise ValueError("opportunity timeframe candles are unavailable")
        latest_closed = timeframe_rows[-1]
        candle_age = (stamp - latest_closed.close_time.astimezone(timezone.utc)).total_seconds()
        if candle_age < -2 or candle_age > timeframe_age:
            raise ValueError("opportunity timeframe candle is stale or future-dated")

        policy = next(
            (item for item in paper_service.config.symbols if item.symbol == symbol),
            None,
        )
        if policy is None:
            raise ValueError("opportunity symbol has no configured paper cost policy")
        cost_pct = getattr(policy, "estimated_round_trip_cost_pct", None)
        if (
            not isinstance(cost_pct, (int, float)) or isinstance(cost_pct, bool)
            or not math.isfinite(cost_pct) or cost_pct < 0
        ):
            raise ValueError("configured round-trip cost is invalid")

        market_assessment = assess_market(symbol, candles, now=stamp)
        indicators = {
            interval: assess_indicators(symbol, interval, rows, now=stamp)
            for interval, rows in candles.items()
        }
        strategy_candidate = StrategyCandidate(
            opportunity_id=record.opportunity_id,
            symbol=symbol,
            market_type="FUTURES",
            market_assessment=market_assessment,
            spread_bps=float(spread_bps),
            quote_volume_24h=float(quote_volume),
            estimated_round_trip_cost_bps=float(cost_pct) * 100.0,
            assessment_version=max(1, record.version + 1),
            orders_authorized=False,
            indicator_assessments=indicators,
        )
        engine = strategy_engine or MultiStrategyAssessmentEngine()
        assessments = engine.assess(strategy_candidate, refs, now=stamp)
        target = next((item for item in assessments if item.strategy_id == record.strategy_id), None)
        evidence_version = _append_revalidation_evidence(
            coordinator, record, refs=refs, assessments=assessments,
            now=stamp, market_type="FUTURES", mark_price=float(mark_price),
            mark_observed_at=mark_at.astimezone(timezone.utc),
        )
        if (
            target is None or target.decision != "ELIGIBLE"
            or target.payload.get("eligible_for_risk_review") is not True
            or target.payload.get("direction") != record.direction
        ):
            latest = _transition_blocked(
                coordinator, record.opportunity_id,
                reason="fresh independent strategy revalidation no longer confirms the triggered direction",
                now=stamp, evidence_version=evidence_version, target_state=(
                    "NO_TRADE" if record.state == "TRIGGERED" else "BLOCKED"
                ),
            )
            return {"status": "REVALIDATION_REJECTED", "state": latest.state, "orders_authorized": False}

        research_rows = tuple(
            StrategyCandle(
                symbol=symbol, timestamp=item.close_time,
                open=item.open, high=item.high, low=item.low,
                close=item.close, volume=item.volume,
            )
            for item in timeframe_rows
        )
        research = research_strategies(
            symbol, research_rows, now=stamp,
            config=ResearchConfig(max_candle_age_seconds=timeframe_age),
        )
        if research.decision != f"REVIEW_{record.direction}":
            latest = _transition_blocked(
                coordinator, record.opportunity_id,
                reason="fresh independent strategy research did not confirm the triggered direction",
                now=stamp, evidence_version=evidence_version,
                target_state="NO_TRADE" if record.state == "TRIGGERED" else "BLOCKED",
            )
            return {"status": "RESEARCH_REJECTED", "state": latest.state, "orders_authorized": False}

        candidate = ResearchCandidate(
            symbol=symbol,
            scanner_score=float(target.payload.get("fit_score") or 0.0),
            status="REVIEW",
            direction=record.direction,
            reasons=tuple(str(item) for item in target.payload.get("reasons", ())),
            opportunity=None,
            strategy_research=research,
            liquidity=None,
            orders_authorized=False,
        )
        remaining_lifetime = (record.expires_at - stamp).total_seconds()
        if remaining_lifetime < 30:
            raise ValueError("triggered opportunity has insufficient lifetime for a fresh paper handoff")
        lifetime = min(
            max(30.0, float(paper_service.config.plan_lifetime_seconds)),
            remaining_lifetime,
        )
        planned = plan_prices(
            PricePlanRequest(
                signal_id=signal_id,
                symbol=symbol,
                direction=record.direction,
                reference_price=float(mark_price),
                reference_price_at=mark_at,
                candles=tuple(timeframe_rows),
                now=stamp,
                expires_at=stamp + timedelta(seconds=lifetime),
                estimated_round_trip_cost_pct=float(cost_pct),
                tick_size=float(contract.tick_size),
            ),
            PricePlanningConfig(
                max_reference_age_seconds=30,
                max_candle_age_seconds=timeframe_age,
                max_round_trip_cost_pct=max(0.5, float(cost_pct)),
            ),
        )
        if planned.status != "READY" or planned.plan is None:
            latest = _transition_blocked(
                coordinator, record.opportunity_id,
                reason=f"fresh ATR price plan failed ({planned.status})",
                now=stamp, evidence_version=evidence_version,
                target_state="BLOCKED",
            )
            return {"status": "NO_FRESH_PLAN", "state": latest.state, "orders_authorized": False}

        # The original trigger is already confirmed. Reprice entry/SL/TP at the
        # current mark while retaining the trigger condition and its audit identity.
        handoff_plan = replace(
            planned.plan,
            trigger_price=watch.trigger_price,
            trigger_rule=watch.trigger_rule,
            invalidation_price=watch.invalidation_price,
        )
        handoff = prepare_signal(candidate, handoff_plan, now=stamp)
        if handoff.status != "VALID" or handoff.watch_request is None:
            latest = _transition_blocked(
                coordinator, record.opportunity_id,
                reason="fresh signal handoff validation rejected the triggered opportunity",
                now=stamp, evidence_version=evidence_version,
                target_state="BLOCKED",
            )
            return {"status": "SIGNAL_REJECTED", "state": latest.state, "orders_authorized": False}

        latest = coordinator.store.get_opportunity(record.opportunity_id)
        if latest is None:
            raise RuntimeError("opportunity disappeared before paper risk validation")
        if latest.state == "TRIGGERED":
            latest = coordinator.store.transition(
                latest.opportunity_id,
                expected_version=latest.version,
                to_state="RISK_CHECK",
                reason="fresh five-strategy and signal validation passed; entering independent Risk Guardian",
                event_id="event-risk-check-" + hashlib.sha256(
                    f"{latest.opportunity_id}|{evidence_version}".encode("utf-8")
                ).hexdigest()[:40],
                actor="signal_validator",
                evidence_version=evidence_version,
                now=stamp,
            ).opportunity
        elif latest.state != "RISK_CHECK":
            return {"status": "STATE_CONFLICT", "state": latest.state, "orders_authorized": False}

        cycle_result = paper_service.process_validated_handoff(
            candidate=candidate,
            handoff=handoff,
            tick=MarketTick(
                symbol=symbol, price=float(mark_price),
                observed_at=mark_at.astimezone(timezone.utc),
                candle_close=float(latest_closed.close),
            ),
            contract=contract,
            now=stamp,
        )
        orchestration = getattr(cycle_result, "orchestration", None)
        paper_intent = getattr(orchestration, "paper_intent", None)
        paper_status = getattr(orchestration, "status", getattr(cycle_result, "status", "UNKNOWN"))
        proposal_id = getattr(paper_intent, "proposal_id", None)
        ledger_orders = tuple(paper_service.ledger.orders)
        ledger_order = next(
            (item for item in ledger_orders if item.proposal_id == f"paper:{signal_id}"),
            None,
        )
        if paper_status in {"PAPER_ORDER_RECORDED", "DUPLICATE_IGNORED"} and ledger_order is not None:
            intent_evidence = OpportunityEvidenceRecord(
                evidence_id="evidence-paper-intent-" + hashlib.sha256(
                    f"{record.opportunity_id}|{ledger_order.proposal_id}".encode("utf-8")
                ).hexdigest()[:40],
                opportunity_id=record.opportunity_id,
                evidence_type="PAPER_INTENT",
                reference_id=ledger_order.proposal_id,
                observed_at=stamp,
                payload={
                    "proposal_id": ledger_order.proposal_id,
                    "signal_id": ledger_order.signal_id,
                    "ledger_status": ledger_order.status,
                    "risk_approved": True,
                    "orders_authorized": False,
                },
                orders_authorized=False,
            )
            coordinator.store.append_evidence(
                intent_evidence,
                idempotency_key=f"paper-intent:{record.opportunity_id}:{ledger_order.proposal_id}",
                now=stamp,
            )
            current = coordinator.store.get_opportunity(record.opportunity_id)
            if current is not None and current.state == "RISK_CHECK":
                current = coordinator.store.transition(
                    current.opportunity_id,
                    expected_version=current.version,
                    to_state="INTENT_RECORDED",
                    reason="Risk Guardian approved and the durable paper ledger confirmed the intent",
                    event_id="event-intent-recorded-" + hashlib.sha256(
                        f"{record.opportunity_id}|{ledger_order.proposal_id}".encode("utf-8")
                    ).hexdigest()[:40],
                    actor="risk_manager",
                    evidence_version=ledger_order.proposal_id,
                    now=stamp,
                ).opportunity
            return {
                "status": "INTENT_RECORDED",
                "state": current.state if current else "UNKNOWN",
                "proposal_id": ledger_order.proposal_id,
                "orders_authorized": False,
            }

        # A ledger write can be uncertain after a transport error. Leave RISK_CHECK
        # for the reconciler instead of inventing a failure or retrying a new order.
        if paper_status in {"PIPELINE_ERROR", "PAPER_CYCLE_REJECTED"}:
            return {
                "status": "RECONCILIATION_REQUIRED",
                "state": "RISK_CHECK",
                "reason": "paper cycle outcome requires durable ledger reconciliation",
                "orders_authorized": False,
            }
        decision_evidence = OpportunityEvidenceRecord(
            evidence_id="evidence-risk-decision-" + hashlib.sha256(
                f"{record.opportunity_id}|{paper_status}".encode("utf-8")
            ).hexdigest()[:40],
            opportunity_id=record.opportunity_id,
            evidence_type="RISK_DECISION",
            reference_id=f"risk-decision:{record.opportunity_id}:{paper_status}",
            observed_at=stamp,
            payload={
                "status": paper_status,
                "reason": str(getattr(cycle_result, "reason", "paper risk validation rejected"))[:300],
                "orders_authorized": False,
            },
            orders_authorized=False,
        )
        coordinator.store.append_evidence(
            decision_evidence,
            idempotency_key=f"risk-decision:{record.opportunity_id}:{paper_status}",
            now=stamp,
        )
        latest = _transition_blocked(
            coordinator, record.opportunity_id,
            reason="independent Risk Guardian or paper ledger rejected the opportunity",
            now=stamp, evidence_version=evidence_version, target_state="BLOCKED",
        )
        return {
            "status": "RISK_VETOED",
            "state": latest.state,
            "paper_status": paper_status,
            "orders_authorized": False,
        }
    except Exception as exc:
        latest = coordinator.store.get_opportunity(record.opportunity_id)
        if latest is not None and latest.state in {"TRIGGERED", "RISK_CHECK"}:
            # If an order was written but the response was lost, the reconciler
            # will find it by the deterministic proposal ID before any retry.
            try:
                orders = tuple(paper_service.ledger.orders) if paper_service is not None else ()
                expected_proposal = f"paper:{_signal_id(record.opportunity_id)}"
                if any(item.proposal_id == expected_proposal for item in orders):
                    return {
                        "status": "RECONCILIATION_REQUIRED",
                        "state": latest.state,
                        "reason": "paper ledger contains the deterministic intent; reconciliation is pending",
                        "orders_authorized": False,
                    }
            except Exception:
                return {
                    "status": "RECONCILIATION_REQUIRED",
                    "state": latest.state,
                    "reason": "paper ledger state is uncertain; no retry was attempted",
                    "orders_authorized": False,
                }
            latest = _transition_blocked(
                coordinator, record.opportunity_id,
                reason=f"trigger handoff failed closed ({type(exc).__name__})",
                now=stamp,
            )
            return {
                "status": "HANDOFF_FAILED",
                "state": latest.state,
                "reason": type(exc).__name__,
                "orders_authorized": False,
            }
        return {
            "status": "HANDOFF_FAILED",
            "state": latest.state if latest else "UNKNOWN",
            "reason": type(exc).__name__,
            "orders_authorized": False,
        }


def reconcile_paper_intents(
    coordinator: OpportunityCoordinator, ledger: Any, *, now: datetime,
) -> tuple[dict[str, Any], ...]:
    """Recover ledger-confirmed intent/open transitions after worker interruption."""
    if not isinstance(coordinator, OpportunityCoordinator):
        raise ValueError("a durable opportunity coordinator is required")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("reconciliation clock must be timezone-aware")
    stamp = now.astimezone(timezone.utc)
    page = coordinator.store.list_opportunities(
        states=("RISK_CHECK", "INTENT_RECORDED", "PAPER_OPEN"),
        market_type=coordinator.market_type,
        venue=coordinator.venue,
        limit=100,
    )
    if page.next_cursor is not None:
        raise RuntimeError("paper opportunity reconciliation exceeded its bounded page")
    orders = tuple(getattr(ledger, "orders", ()))
    positions = tuple(getattr(ledger, "positions", ()))
    results: list[dict[str, Any]] = []
    for record in page.items:
        proposal_id = f"paper:{_signal_id(record.opportunity_id)}"
        order = next((item for item in orders if item.proposal_id == proposal_id), None)
        position = next((item for item in positions if item.proposal_id == proposal_id), None)
        current = record
        if current.state == "RISK_CHECK" and (order is not None or position is not None):
            intent_status = getattr(order, "status", "POSITION_CONFIRMED")
            coordinator.store.append_evidence(
                OpportunityEvidenceRecord(
                    evidence_id="evidence-paper-intent-" + hashlib.sha256(
                        f"{record.opportunity_id}|{proposal_id}".encode("utf-8")
                    ).hexdigest()[:40],
                    opportunity_id=record.opportunity_id,
                    evidence_type="PAPER_INTENT",
                    reference_id=proposal_id,
                    observed_at=stamp,
                    payload={
                        "proposal_id": proposal_id,
                        "signal_id": _signal_id(record.opportunity_id),
                        "ledger_status": intent_status,
                        "risk_approved": True,
                        "orders_authorized": False,
                    },
                    orders_authorized=False,
                ),
                idempotency_key=f"paper-intent:{record.opportunity_id}:{proposal_id}",
                now=stamp,
            )
            current = coordinator.store.transition(
                current.opportunity_id,
                expected_version=current.version,
                to_state="INTENT_RECORDED",
                reason="recovered Risk Guardian-approved intent from the durable paper ledger",
                event_id="event-intent-recovered-" + hashlib.sha256(
                    f"{record.opportunity_id}|{proposal_id}".encode("utf-8")
                ).hexdigest()[:40],
                actor="risk_manager",
                evidence_version=proposal_id,
                now=stamp,
            ).opportunity
        if current.state in {"INTENT_RECORDED", "PAPER_OPEN"} and position is not None:
            position_status = getattr(position, "status", None)
            if position_status not in {"OPEN", "CLOSED"}:
                continue
            target_state = "PAPER_OPEN" if position_status == "OPEN" else "PAPER_CLOSED"
            coordinator.store.append_evidence(
                OpportunityEvidenceRecord(
                    evidence_id="evidence-paper-position-" + hashlib.sha256(
                        f"{record.opportunity_id}|{proposal_id}|{position_status}".encode("utf-8")
                    ).hexdigest()[:40],
                    opportunity_id=record.opportunity_id,
                    evidence_type="PAPER_POSITION",
                    reference_id=proposal_id,
                    observed_at=stamp,
                    payload={
                        "proposal_id": proposal_id,
                        "status": position_status,
                        "opened_at": (
                            position.opened_at.isoformat()
                            if isinstance(getattr(position, "opened_at", None), datetime) else None
                        ),
                        "closed_at": (
                            position.closed_at.isoformat()
                            if isinstance(getattr(position, "closed_at", None), datetime) else None
                        ),
                        "realized_pnl": getattr(position, "realized_pnl", None),
                        "orders_authorized": False,
                    },
                    orders_authorized=False,
                ),
                idempotency_key=f"paper-position:{record.opportunity_id}:{proposal_id}:{position_status}",
                now=stamp,
            )
            if current.state != target_state:
                current = coordinator.store.transition(
                    current.opportunity_id,
                    expected_version=current.version,
                    to_state=target_state,
                    reason=(
                        "durable paper ledger confirms the simulated position was opened"
                        if target_state == "PAPER_OPEN"
                        else "durable paper ledger confirms the simulated position was closed"
                    ),
                    event_id=f"event-paper-{position_status.lower()}-" + hashlib.sha256(
                        f"{record.opportunity_id}|{proposal_id}".encode("utf-8")
                    ).hexdigest()[:40],
                    actor="paper_ledger",
                    evidence_version=proposal_id,
                    now=stamp,
                ).opportunity
        results.append({
            "opportunity_id": record.opportunity_id,
            "state": current.state,
            "proposal_id": proposal_id if order is not None or position is not None else None,
            "orders_authorized": False,
        })
    return tuple(results)