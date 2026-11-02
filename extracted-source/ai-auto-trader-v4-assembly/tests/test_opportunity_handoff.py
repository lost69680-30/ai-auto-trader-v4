from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import hashlib
import os

import pytest

from backend.market_data_adapter import MarketCandle, MarketUniverse
from backend.opportunity_coordinator import OpportunityCoordinator
from backend.opportunity_handoff import process_triggered_opportunity, reconcile_paper_intents
from backend.opportunity_store import (
    OpportunityRecord, PostgresOpportunityStore, StrategyAssessmentRecord, WatchConditionRecord,
)
from backend.scanner import Contract, MarketSnapshot
from backend.strategy_profiles import DEFAULT_STRATEGY_PROFILES


DSN = os.environ.get("PAPER_TEST_DATABASE_URL")


def _database_available():
    if not DSN:
        return False
    try:
        import psycopg
        with psycopg.connect(DSN) as conn:
            return conn.execute("SELECT 1").fetchone() == (1,)
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _database_available(),
    reason="PAPER_TEST_DATABASE_URL is not configured or PostgreSQL is unavailable",
)


def reset_registry():
    import psycopg

    root = Path(__file__).resolve().parents[3]
    migrations = (
        root / "supabase/migrations/20261029205318_create_paper_ledger_schema.sql",
        root / "supabase/migrations/20261030195452_create_shared_market_data.sql",
        root / "supabase/migrations/20261030195505_create_durable_scheduled_jobs.sql",
        root / "supabase/migrations/20261031155011_create_market_intelligence_reports.sql",
        root / "supabase/migrations/20261031170000_create_opportunity_registry.sql",
    )
    with psycopg.connect(DSN, autocommit=True) as conn:
        for migration in migrations:
            conn.execute(migration.read_text(encoding="utf-8"), prepare=False)
        conn.execute(
            """TRUNCATE paper_ledger.opportunity_claims,
                         paper_ledger.opportunity_watch_conditions,
                         paper_ledger.opportunity_evidence,
                         paper_ledger.opportunity_assessments,
                         paper_ledger.opportunity_events,
                         paper_ledger.opportunities"""
        )


def _triggered_opportunity(store, now):
    record = OpportunityRecord(
        opportunity_id="opp-handoff-" + hashlib.sha256(str(now.timestamp()).encode()).hexdigest()[:24],
        symbol="BTCUSDT",
        market_type="FUTURES",
        venue="BINANCE_USDM_TESTNET",
        strategy_id="swing",
        strategy_version="1.0.0",
        setup_fingerprint="setup-handoff-" + hashlib.sha256(str(now.timestamp()).encode()).hexdigest()[:20],
        direction="LONG",
        timeframe="1h",
        expires_at=now + timedelta(hours=1),
        metadata={
            "price_plan": {
                "signal_id": "original-signal",
                "direction": "LONG",
                "entry_price": 150.0,
                "stop_price": 149.0,
                "target_price": 152.0,
                "trigger_price": 150.1,
                "trigger_rule": "AT_OR_ABOVE",
                "invalidation_price": 149.0,
                "estimated_round_trip_cost_pct": 0.05,
                "orders_authorized": False,
            },
            "orders_authorized": False,
        },
        orders_authorized=False,
    )
    store.create_or_get_opportunity(record, idempotency_key=record.opportunity_id, now=now)
    for version, state, actor in (
        (0, "ASSESSING", "assessment_engine"),
        (1, "WAITING", "opportunity_coordinator"),
        (2, "TRIGGERED", "opportunity_coordinator"),
    ):
        store.transition(
            record.opportunity_id, expected_version=version, to_state=state,
            reason="test lifecycle", event_id=f"{record.opportunity_id}-{state}",
            actor=actor, evidence_version="test-evidence", now=now,
        )
    store.save_watch_condition(
        WatchConditionRecord(
            watch_id=f"watch-{record.opportunity_id}",
            opportunity_id=record.opportunity_id,
            trigger_rule="AT_OR_ABOVE",
            trigger_price=150.1,
            invalidation_price=149.0,
            timeframe="1h",
            expires_at=record.expires_at,
            payload={"orders_authorized": False},
            orders_authorized=False,
        ),
        idempotency_key=f"watch-{record.opportunity_id}",
        now=now,
    )
    return store.get_opportunity(record.opportunity_id)


class RevalidationEngine:
    def __init__(self, *, eligible=True):
        self.eligible = eligible

    def assess(self, candidate, evidence, *, now):
        records = []
        for profile in DEFAULT_STRATEGY_PROFILES:
            eligible = self.eligible and profile.strategy_id == "swing"
            decision = (
                "ELIGIBLE" if eligible
                else "ANALYSIS_ONLY" if profile.analysis_only
                else "NO_TRADE"
            )
            records.append(StrategyAssessmentRecord(
                assessment_id=f"revalidation-{candidate.opportunity_id}-{profile.strategy_id}-{candidate.assessment_version}",
                opportunity_id=candidate.opportunity_id,
                strategy_id=profile.strategy_id,
                strategy_version=profile.strategy_version,
                assessment_version=candidate.assessment_version,
                decision=decision,
                payload={
                    "strategy_id": profile.strategy_id,
                    "strategy_version": profile.strategy_version,
                    "direction": "LONG" if eligible else "NONE",
                    "required_timeframes": list(profile.required_timeframes),
                    "analysis_only": profile.analysis_only,
                    "eligible_for_risk_review": eligible,
                    "paper_eligible": eligible,
                    "fit_score": 80.0 if eligible else None,
                    "reasons": ["fresh revalidation test"],
                    "orders_authorized": False,
                },
                evidence_refs=list(evidence),
                orders_authorized=False,
            ))
        return tuple(records)


class FreshSource:
    def __init__(self, now):
        self.now = now
        self.contract = Contract(
            "BTCUSDT", "TRADING", "PERPETUAL", "USDT",
            tick_size=0.01, quantity_step=0.001, min_quantity=0.001,
        )
        self.snapshot = MarketSnapshot(
            "BTCUSDT", now, 150.15, 20_000_000.0, 2.0, 1.0,
        )

    def fetch_market_universe(self, *, now=None):
        return MarketUniverse((self.contract,), (self.snapshot,), self.now, False)

    def fetch_mark_price(self, symbol):
        return SimpleNamespace(
            symbol=symbol, mark_price=150.15, observed_at=self.now,
            market_type="FUTURES", orders_authorized=False,
        )

    def fetch_klines(self, symbol, *, interval, limit, now=None):
        if interval != "1h":
            return ()
        rows = []
        for index in range(100):
            opened = self.now - timedelta(hours=100 - index)
            close = 100.0 + index * 0.5
            rows.append(MarketCandle(
                symbol=symbol, open_time=opened,
                close_time=opened + timedelta(hours=1) - timedelta(seconds=1),
                open=close - 0.1, high=close + 0.5, low=close - 0.5,
                close=close, volume=100.0, quote_volume=15_000.0,
                trades=100, is_closed=True,
            ))
        return tuple(rows)


class FakePaperService:
    def __init__(self):
        self.config = SimpleNamespace(
            symbols=(SimpleNamespace(symbol="BTCUSDT", estimated_round_trip_cost_pct=0.05),),
            plan_lifetime_seconds=20,
        )
        self.ledger = SimpleNamespace(orders=(), positions=())
        self.calls = []

    def process_validated_handoff(self, *, candidate, handoff, tick, contract, now):
        self.calls.append((candidate, handoff, tick, contract))
        proposal_id = f"paper:{handoff.signal.signal_id}"
        self.ledger.orders = (SimpleNamespace(
            proposal_id=proposal_id,
            signal_id=handoff.signal.signal_id,
            status="PENDING_PAPER_SIMULATION",
        ),)
        return SimpleNamespace(
            status="PAPER_ORDER_RECORDED",
            reason="test paper intent recorded",
            orchestration=SimpleNamespace(
                status="PAPER_ORDER_RECORDED",
                paper_intent=SimpleNamespace(proposal_id=proposal_id),
            ),
        )


def _coordinator(store):
    return OpportunityCoordinator(
        store, owner="handoff-test", market_type="FUTURES",
        venue="BINANCE_USDM_TESTNET",
    )


def test_triggered_futures_opportunity_revalidates_then_uses_paper_handoff_and_risk_state():
    reset_registry()
    now = datetime.now(timezone.utc)
    store = PostgresOpportunityStore(DSN)
    coordinator = _coordinator(store)
    record = _triggered_opportunity(store, now)
    paper = FakePaperService()

    result = process_triggered_opportunity(
        coordinator, record, source=FreshSource(now), paper_service=paper,
        now=now, strategy_engine=RevalidationEngine(),
    )

    saved = store.get_opportunity(record.opportunity_id)
    assert result["status"] == "INTENT_RECORDED"
    assert saved.state == "INTENT_RECORDED"
    assert len(paper.calls) == 1
    assert paper.calls[0][1].status == "VALID"
    assert paper.calls[0][1].orders_authorized is False
    assert any(item.evidence_type == "TRIGGER_REVALIDATION" for item in store.list_evidence(record.opportunity_id))
    assert any(item.evidence_type == "PAPER_INTENT" for item in store.list_evidence(record.opportunity_id))
    assert saved.orders_authorized is False


def test_triggered_opportunity_is_rejected_when_original_strategy_no_longer_qualifies():
    reset_registry()
    now = datetime.now(timezone.utc)
    store = PostgresOpportunityStore(DSN)
    coordinator = _coordinator(store)
    record = _triggered_opportunity(store, now)
    paper = FakePaperService()

    result = process_triggered_opportunity(
        coordinator, record, source=FreshSource(now), paper_service=paper,
        now=now, strategy_engine=RevalidationEngine(eligible=False),
    )

    assert result["status"] == "REVALIDATION_REJECTED"
    assert store.get_opportunity(record.opportunity_id).state == "NO_TRADE"
    assert paper.calls == []


def test_spot_trigger_never_reuses_futures_paper_risk_context():
    reset_registry()
    now = datetime.now(timezone.utc)
    store = PostgresOpportunityStore(DSN)
    coordinator = OpportunityCoordinator(
        store, owner="spot-handoff-test", market_type="SPOT",
        venue="BINANCE_SPOT_TESTNET",
    )
    record = OpportunityRecord(
        opportunity_id="opp-spot-handoff",
        symbol="BTCUSDT", market_type="SPOT", venue="BINANCE_SPOT_TESTNET",
        strategy_id="swing", strategy_version="1.0.0",
        setup_fingerprint="spot-handoff-setup", direction="LONG",
        timeframe="1h", state="TRIGGERED", version=0,
        expires_at=now + timedelta(hours=1),
        metadata={"orders_authorized": False}, orders_authorized=False,
    )
    paper = FakePaperService()

    result = process_triggered_opportunity(
        coordinator, record, source=FreshSource(now), paper_service=paper, now=now,
    )

    assert result["status"] == "SPOT_RESEARCH_ONLY"
    assert paper.calls == []


def test_restart_reconciliation_records_intent_then_ledger_confirmed_position():
    reset_registry()
    now = datetime.now(timezone.utc)
    store = PostgresOpportunityStore(DSN)
    coordinator = _coordinator(store)
    record = _triggered_opportunity(store, now)
    store.transition(
        record.opportunity_id, expected_version=3, to_state="RISK_CHECK",
        reason="test risk check", event_id="test-risk-check",
        actor="signal_validator", evidence_version="test-revalidation", now=now,
    )
    signal_id = "opportunity-" + hashlib.sha256(record.opportunity_id.encode("utf-8")).hexdigest()[:40]
    proposal_id = f"paper:{signal_id}"
    ledger = SimpleNamespace(
        orders=(SimpleNamespace(proposal_id=proposal_id, signal_id=signal_id, status="PENDING_PAPER_SIMULATION"),),
        positions=(),
    )

    first = reconcile_paper_intents(coordinator, ledger, now=now)
    assert first[0]["state"] == "INTENT_RECORDED"
    assert store.get_opportunity(record.opportunity_id).state == "INTENT_RECORDED"

    ledger.positions = (SimpleNamespace(
        proposal_id=proposal_id, status="OPEN",
        opened_at=now, closed_at=None, realized_pnl=0.0,
    ),)
    second = reconcile_paper_intents(coordinator, ledger, now=now + timedelta(seconds=5))
    assert second[0]["state"] == "PAPER_OPEN"
    assert store.get_opportunity(record.opportunity_id).state == "PAPER_OPEN"
    assert any(item.evidence_type == "PAPER_POSITION" for item in store.list_evidence(record.opportunity_id))
