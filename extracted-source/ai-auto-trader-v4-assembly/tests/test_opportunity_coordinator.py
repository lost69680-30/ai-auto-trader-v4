from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import uuid

import pytest

from app.engines.price_planner import PricePlanResult
from app.engines.signal_handoff import SignalPricePlan
from backend.opportunity_coordinator import OpportunityCoordinator
from backend.opportunity_store import PostgresOpportunityStore, StrategyAssessmentRecord
from backend.signal_watcher import MarketTick, WatcherConfig


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


def assessment(*, strategy_id="swing", direction="LONG", decision="ELIGIBLE", **payload_overrides):
    required = {
        "scalping": ["1m", "5m", "15m", "1h"],
        "day": ["5m", "15m", "1h", "4h"],
        "swing": ["1h", "4h", "1d", "1w"],
        "position": ["1d", "1w", "1M"],
        "long_term": ["1w", "1M", "1Y"],
    }[strategy_id]
    payload = {
        "strategy_id": strategy_id,
        "strategy_version": "1.0.0",
        "direction": direction,
        "required_timeframes": required,
        "calibration_status": "NOT_CALIBRATED",
        "analysis_only": strategy_id == "long_term",
        "eligible_for_risk_review": decision == "ELIGIBLE" and strategy_id != "long_term",
        "paper_eligible": decision == "ELIGIBLE" and strategy_id != "long_term",
        "orders_authorized": False,
    }
    payload.update(payload_overrides)
    return StrategyAssessmentRecord(
        assessment_id=f"scan-assessment-{uuid.uuid4().hex}",
        opportunity_id="scan-candidate-123",
        strategy_id=strategy_id,
        strategy_version="1.0.0",
        assessment_version=1,
        decision=decision,
        payload=payload,
        evidence_refs=["snapshot:BTCUSDT:latest"],
        orders_authorized=False,
    )


def price_plan(*, trigger=101.0, signal_id="signal-1", direction="LONG", expires=None, now=None):
    now = now or datetime.now(timezone.utc)
    expires = expires or now + timedelta(minutes=10)
    plan = SignalPricePlan(
        signal_id=signal_id,
        direction=direction,
        created_at=now,
        expires_at=expires,
        market_data_at=now,
        entry_price=100.0,
        stop_price=95.0 if direction == "LONG" else 105.0,
        target_price=110.0 if direction == "LONG" else 90.0,
        estimated_round_trip_cost_pct=0.05,
        trigger_price=trigger,
        trigger_rule="AT_OR_ABOVE" if direction == "LONG" else "AT_OR_BELOW",
        invalidation_price=95.0 if direction == "LONG" else 105.0,
    )
    return PricePlanResult(
        status="READY", reason="test plan is explicitly ready",
        plan=plan, atr=2.0, net_reward_to_risk=2.0,
        orders_authorized=False,
    )


def coordinator(store):
    return OpportunityCoordinator(
        store,
        owner="test-opportunity-coordinator",
        watcher_config=WatcherConfig(max_market_data_age_seconds=10),
        lease_seconds=30,
    )


def register(engine, store, *, result=None, trigger=101.0, signal_id="signal-1", timeframe="1h", expires=None, now=None):
    now = now or datetime.now(timezone.utc)
    result = result or assessment()
    return engine.register_assessed_opportunity(
        result,
        price_plan_result=price_plan(trigger=trigger, signal_id=signal_id, expires=expires, now=now),
        symbol="BTCUSDT",
        market_type="FUTURES",
        venue="BINANCE_USDM_TESTNET",
        timeframe=timeframe,
        evidence_refs=["snapshot:BTCUSDT:latest", "candle:BTCUSDT:1h:closed"],
        now=now,
    )


def test_valid_assessment_and_explicit_price_plan_create_durable_waiting_opportunity():
    reset_registry()
    now = datetime.now(timezone.utc)
    store = PostgresOpportunityStore(DSN)
    engine = coordinator(store)

    created = register(engine, store, now=now)

    assert created.state == "WAITING"
    assert created.version == 2
    assert created.orders_authorized is False
    assert created.metadata["paper_eligible"] is True
    assert store.get_watch_condition(created.opportunity_id) is not None
    saved_assessments = store.list_assessments(created.opportunity_id)
    assert len(saved_assessments) == 1
    assert saved_assessments[0].orders_authorized is False
    assert [event.to_state for event in store.list_events(created.opportunity_id)] == [
        "DISCOVERED", "ASSESSING", "WAITING"
    ]


def test_same_setup_is_idempotent_but_distinct_price_triggers_create_separate_opportunities():
    reset_registry()
    now = datetime.now(timezone.utc)
    store = PostgresOpportunityStore(DSN)
    engine = coordinator(store)

    first = register(engine, store, now=now)
    repeated = register(engine, store, now=now)
    second = register(engine, store, trigger=103.0, signal_id="signal-2", now=now)

    assert repeated.opportunity_id == first.opportunity_id
    assert second.opportunity_id != first.opportunity_id
    assert len(store.list_opportunities(symbol="BTCUSDT", limit=20).items) == 2


def test_long_term_analysis_and_noneligible_assessments_cannot_create_watches():
    reset_registry()
    now = datetime.now(timezone.utc)
    store = PostgresOpportunityStore(DSN)
    engine = coordinator(store)

    with pytest.raises(ValueError, match="analysis-only|long-term"):
        register(engine, store, result=assessment(strategy_id="long_term", decision="ANALYSIS_ONLY"), now=now)

    with pytest.raises(ValueError, match="eligible"):
        register(engine, store, result=assessment(decision="REQUEST_MORE_EVIDENCE"), now=now)

    assert store.list_opportunities(limit=20).items == ()


def test_missing_or_nonready_price_plan_fails_closed_without_registering_opportunity():
    reset_registry()
    now = datetime.now(timezone.utc)
    store = PostgresOpportunityStore(DSN)
    engine = coordinator(store)
    failed = PricePlanResult("NO_PLAN", "insufficient candles", None, orders_authorized=False)

    with pytest.raises(ValueError, match="READY"):
        engine.register_assessed_opportunity(
            assessment(), price_plan_result=failed, symbol="BTCUSDT",
            market_type="FUTURES", venue="BINANCE_USDM_TESTNET",
            timeframe="1h", evidence_refs=["snapshot:BTCUSDT:latest"], now=now,
        )

    assert store.list_opportunities(limit=20).items == ()


def test_multiple_watches_for_same_symbol_are_evaluated_independently():
    reset_registry()
    now = datetime.now(timezone.utc)
    store = PostgresOpportunityStore(DSN)
    engine = coordinator(store)
    first = register(engine, store, trigger=101.0, signal_id="signal-first", now=now)
    second = register(engine, store, trigger=105.0, signal_id="signal-second", now=now)

    results = engine.evaluate_market_update(
        "BTCUSDT", MarketTick("BTCUSDT", 102.0, now, candle_close=102.0), now=now,
    )
    by_id = {item.opportunity_id: item for item in results}

    assert by_id[first.opportunity_id].state == "TRIGGERED"
    assert by_id[second.opportunity_id].state == "WAITING"
    assert store.get_opportunity(first.opportunity_id).state == "TRIGGERED"
    assert store.get_opportunity(second.opportunity_id).state == "WAITING"


def test_stale_market_update_never_triggers_and_leaves_opportunity_waiting():
    reset_registry()
    now = datetime.now(timezone.utc)
    store = PostgresOpportunityStore(DSN)
    engine = coordinator(store)
    created = register(engine, store, now=now)

    result = engine.evaluate_market_update(
        "BTCUSDT",
        MarketTick("BTCUSDT", 110.0, now - timedelta(minutes=1), candle_close=110.0),
        now=now,
    )

    assert result[0].status == "WAIT_RECHECK"
    assert store.get_opportunity(created.opportunity_id).state == "WAITING"


def test_invalidation_and_expiry_are_terminal_and_audited():
    reset_registry()
    now = datetime.now(timezone.utc)
    store = PostgresOpportunityStore(DSN)
    engine = coordinator(store)
    invalidated = register(
        engine, store, trigger=101.0, signal_id="signal-invalid",
        expires=now + timedelta(minutes=20), now=now,
    )
    expired = register(
        engine, store, trigger=105.0, signal_id="signal-expired",
        expires=now + timedelta(minutes=1), now=now, timeframe="4h",
    )
    later = now + timedelta(minutes=2)
    results = engine.evaluate_market_update(
        "BTCUSDT", MarketTick("BTCUSDT", 94.0, later, candle_close=94.0), now=later,
    )
    by_id = {item.opportunity_id: item for item in results}

    assert by_id[invalidated.opportunity_id].state == "INVALIDATED"
    assert by_id[expired.opportunity_id].state == "EXPIRED"
    assert store.get_opportunity(invalidated.opportunity_id).state == "INVALIDATED"
    assert store.get_opportunity(expired.opportunity_id).state == "EXPIRED"


def test_reassess_due_uses_worker_leases_and_releases_claim_after_trigger():
    reset_registry()
    now = datetime.now(timezone.utc)
    store = PostgresOpportunityStore(DSN)
    engine = coordinator(store)
    created = register(engine, store, now=now)

    run = engine.reassess_due(
        now=now,
        market_update_provider=lambda symbol, stamp: MarketTick(symbol, 102.0, stamp, candle_close=102.0),
        limit=10,
    )

    assert run.evaluated_count == 1
    assert run.results[0].opportunity_id == created.opportunity_id
    assert run.results[0].state == "TRIGGERED"
    assert store.get_opportunity(created.opportunity_id).state == "TRIGGERED"
    assert store.release_claim(created.opportunity_id, owner="test-opportunity-coordinator") is False
