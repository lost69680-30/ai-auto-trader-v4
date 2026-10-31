from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import uuid

import pytest

from backend.opportunity_store import (
    OpportunityRecord,
    PostgresOpportunityStore,
    StrategyAssessmentRecord,
    OpportunityEvidenceRecord,
    WatchConditionRecord,
)


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


def apply_migrations_and_reset():
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
            """
            TRUNCATE paper_ledger.opportunity_claims,
                     paper_ledger.opportunity_watch_conditions,
                     paper_ledger.opportunity_evidence,
                     paper_ledger.opportunity_assessments,
                     paper_ledger.opportunity_events,
                     paper_ledger.opportunities
            """
        )


def make_opportunity(**overrides):
    values = dict(
        opportunity_id=f"opp-{uuid.uuid4().hex}",
        symbol="BTCUSDT",
        market_type="FUTURES",
        venue="BINANCE_USDM_TESTNET",
        strategy_id="swing",
        strategy_version="1.0.0",
        setup_fingerprint=f"setup-{uuid.uuid4().hex}",
        direction="LONG",
        timeframe="4h",
        state="DISCOVERED",
        version=0,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
        metadata={"source": "unit-test"},
        orders_authorized=False,
    )
    values.update(overrides)
    return OpportunityRecord(**values)


def test_opportunity_and_initial_event_survive_store_restart():
    apply_migrations_and_reset()
    now = datetime.now(timezone.utc)
    record = make_opportunity()
    store = PostgresOpportunityStore(DSN)

    created = store.create_or_get_opportunity(
        record, idempotency_key=f"create:{record.opportunity_id}", now=now
    )
    restarted = PostgresOpportunityStore(DSN)
    loaded = restarted.get_opportunity(record.opportunity_id)

    assert created.opportunity_id == record.opportunity_id
    assert loaded == created
    events = restarted.list_events(record.opportunity_id)
    assert len(events) == 1
    assert events[0].from_state is None
    assert events[0].to_state == "DISCOVERED"
    assert events[0].orders_authorized is False


def test_duplicate_create_is_idempotent_and_does_not_append_an_event():
    apply_migrations_and_reset()
    now = datetime.now(timezone.utc)
    record = make_opportunity()
    store = PostgresOpportunityStore(DSN)

    first = store.create_or_get_opportunity(record, idempotency_key="same-create-key", now=now)
    second = store.create_or_get_opportunity(record, idempotency_key="same-create-key", now=now)

    assert second == first
    assert len(store.list_events(record.opportunity_id)) == 1


def test_distinct_setup_fingerprints_for_same_symbol_remain_separate():
    apply_migrations_and_reset()
    now = datetime.now(timezone.utc)
    store = PostgresOpportunityStore(DSN)
    first = make_opportunity(strategy_id="swing", setup_fingerprint="breakout-4h")
    second = make_opportunity(strategy_id="swing", setup_fingerprint="pullback-4h")

    a = store.create_or_get_opportunity(first, idempotency_key="setup-a", now=now)
    b = store.create_or_get_opportunity(second, idempotency_key="setup-b", now=now)

    assert a.opportunity_id != b.opportunity_id
    assert {a.setup_fingerprint, b.setup_fingerprint} == {"breakout-4h", "pullback-4h"}
    assert len(store.list_opportunities(symbol="BTCUSDT", limit=20).items) == 2


def test_transitions_are_atomic_versioned_and_reject_illegal_or_stale_updates():
    apply_migrations_and_reset()
    now = datetime.now(timezone.utc)
    record = make_opportunity()
    store = PostgresOpportunityStore(DSN)
    store.create_or_get_opportunity(record, idempotency_key="transition-create", now=now)

    with pytest.raises(ValueError, match="transition"):
        store.transition(
            record.opportunity_id, expected_version=0, to_state="PAPER_OPEN",
            reason="must not bypass risk", event_id="illegal-open",
            actor="opportunity_coordinator", evidence_version="e1", now=now,
        )

    transitioned = store.transition(
        record.opportunity_id, expected_version=0, to_state="ASSESSING",
        reason="begin independent assessment", event_id="begin-assessment",
        actor="assessment_engine", evidence_version="e1", now=now,
    )
    assert transitioned.opportunity.version == 1
    assert transitioned.opportunity.state == "ASSESSING"

    with pytest.raises((ValueError, RuntimeError), match="version|stale|conflict"):
        store.transition(
            record.opportunity_id, expected_version=0, to_state="WAITING",
            reason="stale writer", event_id="stale-transition",
            actor="assessment_engine", evidence_version="e1", now=now,
        )

    events = store.list_events(record.opportunity_id)
    assert [event.to_state for event in events] == ["DISCOVERED", "ASSESSING"]


def _create_waiting_opportunity(store, record, *, key, now):
    store.create_or_get_opportunity(record, idempotency_key=f"{key}:create", now=now)
    store.transition(
        record.opportunity_id, expected_version=0, to_state="ASSESSING",
        reason="assessment started", event_id=f"{key}:assessing",
        actor="assessment_engine", evidence_version="e1", now=now,
    )
    store.transition(
        record.opportunity_id, expected_version=1, to_state="WAITING",
        reason="setup is waiting", event_id=f"{key}:waiting",
        actor="opportunity_coordinator", evidence_version="e1", now=now,
    )


def test_concurrent_claims_are_exclusive_and_expired_claim_can_be_reclaimed():
    apply_migrations_and_reset()
    now = datetime.now(timezone.utc)
    record = make_opportunity()
    store = PostgresOpportunityStore(DSN)
    _create_waiting_opportunity(store, record, key="claim", now=now)

    def claim(owner):
        return PostgresOpportunityStore(DSN).claim_due_opportunities(
            owner=owner, now=now, limit=1, lease_seconds=30,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("worker-a", "worker-b")))

    assert sum(len(rows) for rows in results) == 1

    reclaimed = PostgresOpportunityStore(DSN).claim_due_opportunities(
        owner="worker-c", now=now + timedelta(seconds=31), limit=1, lease_seconds=30,
    )
    assert len(reclaimed) == 1
    assert reclaimed[0].opportunity_id == record.opportunity_id


def test_repository_rejects_order_authority_and_unsafe_assessment_or_evidence():
    apply_migrations_and_reset()
    store = PostgresOpportunityStore(DSN)
    now = datetime.now(timezone.utc)

    with pytest.raises(ValueError, match="orders_authorized"):
        make_opportunity(orders_authorized=True)

    record = make_opportunity()
    store.create_or_get_opportunity(record, idempotency_key="safe-create", now=now)

    unsafe_assessment = StrategyAssessmentRecord(
        assessment_id="unsafe-assessment",
        opportunity_id=record.opportunity_id,
        strategy_id="swing",
        strategy_version="1.0.0",
        assessment_version=1,
        decision="WAIT",
        payload={"orders_authorized": True},
        evidence_refs=[],
        orders_authorized=False,
    )
    with pytest.raises(ValueError, match="orders_authorized"):
        store.append_assessment(unsafe_assessment, idempotency_key="unsafe-assessment", now=now)

    unsafe_evidence = OpportunityEvidenceRecord(
        evidence_id="unsafe-evidence",
        opportunity_id=record.opportunity_id,
        evidence_type="CANDLE",
        reference_id="snapshot-1",
        observed_at=now,
        payload={"orders_authorized": True},
        orders_authorized=False,
    )
    with pytest.raises(ValueError, match="orders_authorized"):
        store.append_evidence(unsafe_evidence, idempotency_key="unsafe-evidence", now=now)


def test_assessment_and_watch_condition_are_persisted_idempotently():
    apply_migrations_and_reset()
    now = datetime.now(timezone.utc)
    record = make_opportunity()
    store = PostgresOpportunityStore(DSN)
    _create_waiting_opportunity(store, record, key="assessment", now=now)

    assessment = StrategyAssessmentRecord(
        assessment_id="assessment-1",
        opportunity_id=record.opportunity_id,
        strategy_id="swing",
        strategy_version="1.0.0",
        assessment_version=1,
        decision="WAIT",
        payload={"calibration_status": "NOT_CALIBRATED", "orders_authorized": False},
        evidence_refs=["snapshot:btc:4h:2026-10-31T10:00:00Z"],
        orders_authorized=False,
    )
    first = store.append_assessment(assessment, idempotency_key="assessment-key", now=now)
    second = store.append_assessment(assessment, idempotency_key="assessment-key", now=now)
    assert first == second

    watch = WatchConditionRecord(
        watch_id="watch-1",
        opportunity_id=record.opportunity_id,
        trigger_rule="CANDLE_CLOSE_ABOVE",
        trigger_price=100.0,
        invalidation_price=95.0,
        timeframe="4h",
        expires_at=record.expires_at,
        payload={"orders_authorized": False},
        orders_authorized=False,
    )
    saved = store.save_watch_condition(watch, idempotency_key="watch-key", now=now)
    assert saved == store.get_watch_condition(record.opportunity_id)
    assert saved.orders_authorized is False


def test_new_opportunity_cannot_be_created_in_a_later_lifecycle_state():
    apply_migrations_and_reset()
    store = PostgresOpportunityStore(DSN)
    now = datetime.now(timezone.utc)

    for state in ("ASSESSING", "WAITING", "TRIGGERED", "RISK_CHECK", "INTENT_RECORDED", "PAPER_OPEN"):
        record = make_opportunity(state=state)
        with pytest.raises(ValueError, match="DISCOVERED"):
            store.create_or_get_opportunity(
                record, idempotency_key=f"unsafe-create:{state}", now=now
            )


def test_watch_condition_requires_waiting_non_long_term_opportunity():
    apply_migrations_and_reset()
    store = PostgresOpportunityStore(DSN)
    now = datetime.now(timezone.utc)
    executable = make_opportunity()
    long_term = make_opportunity(strategy_id="long_term", setup_fingerprint="long-term-context")
    store.create_or_get_opportunity(executable, idempotency_key="watch-executable", now=now)
    store.create_or_get_opportunity(long_term, idempotency_key="watch-long-term", now=now)

    def watch_for(record, watch_id):
        return WatchConditionRecord(
            watch_id=watch_id,
            opportunity_id=record.opportunity_id,
            trigger_rule="CANDLE_CLOSE_ABOVE",
            trigger_price=100.0,
            invalidation_price=95.0,
            timeframe="4h",
            expires_at=record.expires_at,
            payload={"orders_authorized": False},
            orders_authorized=False,
        )

    with pytest.raises(ValueError, match="WAITING"):
        store.save_watch_condition(
            watch_for(executable, "premature-watch"),
            idempotency_key="premature-watch-key", now=now,
        )

    _create_waiting_opportunity(store, executable, key="watch-ready", now=now)
    with pytest.raises(ValueError, match="long.term|analysis.only"):
        store.save_watch_condition(
            watch_for(long_term, "long-term-watch"),
            idempotency_key="long-term-watch-key", now=now,
        )


def test_risk_manager_intent_requires_explicit_evidence_version():
    apply_migrations_and_reset()
    now = datetime.now(timezone.utc)
    record = make_opportunity()
    store = PostgresOpportunityStore(DSN)
    store.create_or_get_opportunity(record, idempotency_key="risk-create", now=now)
    transitions = (
        ("ASSESSING", "risk-assessing", "assessment_engine", "e1"),
        ("WAITING", "risk-waiting", "opportunity_coordinator", "e1"),
        ("TRIGGERED", "risk-triggered", "opportunity_coordinator", "e2"),
        ("RISK_CHECK", "risk-check", "signal_validator", "e2"),
    )
    for version, (state, event, actor, evidence) in enumerate(transitions):
        store.transition(
            record.opportunity_id, expected_version=version, to_state=state,
            reason="test lifecycle", event_id=event, actor=actor,
            evidence_version=evidence, now=now,
        )

    with pytest.raises(ValueError, match="Risk Manager"):
        store.transition(
            record.opportunity_id, expected_version=4, to_state="INTENT_RECORDED",
            reason="unapproved intent", event_id="unapproved-intent",
            actor="strategy_agent", evidence_version="e3", now=now,
        )
    with pytest.raises(ValueError, match="evidence"):
        store.transition(
            record.opportunity_id, expected_version=4, to_state="INTENT_RECORDED",
            reason="missing risk evidence", event_id="missing-risk-evidence",
            actor="risk_manager", evidence_version=None, now=now,
        )


def test_long_term_analysis_cannot_enter_waiting_or_trigger_lifecycle():
    apply_migrations_and_reset()
    now = datetime.now(timezone.utc)
    record = make_opportunity(strategy_id="long_term")
    store = PostgresOpportunityStore(DSN)
    store.create_or_get_opportunity(record, idempotency_key="long-term-lifecycle", now=now)
    store.transition(
        record.opportunity_id, expected_version=0, to_state="ASSESSING",
        reason="research-only analysis", event_id="long-term-assessing",
        actor="assessment_engine", evidence_version="e1", now=now,
    )

    with pytest.raises(ValueError, match="long-term analysis"):
        store.transition(
            record.opportunity_id, expected_version=1, to_state="WAITING",
            reason="must remain analysis-only", event_id="long-term-waiting",
            actor="opportunity_coordinator", evidence_version="e1", now=now,
        )


def test_paper_open_requires_ledger_confirmation_after_risk_approved_intent():
    apply_migrations_and_reset()
    now = datetime.now(timezone.utc)
    record = make_opportunity()
    store = PostgresOpportunityStore(DSN)
    store.create_or_get_opportunity(record, idempotency_key="paper-open-create", now=now)
    transitions = (
        ("ASSESSING", "paper-assessing", "assessment_engine", "e1"),
        ("WAITING", "paper-waiting", "opportunity_coordinator", "e1"),
        ("TRIGGERED", "paper-triggered", "opportunity_coordinator", "e2"),
        ("RISK_CHECK", "paper-risk-check", "signal_validator", "e2"),
    )
    for version, (state, event, actor, evidence) in enumerate(transitions):
        store.transition(
            record.opportunity_id, expected_version=version, to_state=state,
            reason="paper-only lifecycle test", event_id=event, actor=actor,
            evidence_version=evidence, now=now,
        )
    store.transition(
        record.opportunity_id, expected_version=4, to_state="INTENT_RECORDED",
        reason="risk manager approved paper intent", event_id="paper-intent-approved",
        actor="risk_manager", evidence_version="risk-evidence-v1", now=now,
    )

    with pytest.raises(ValueError, match="ledger-confirmed"):
        store.transition(
            record.opportunity_id, expected_version=5, to_state="PAPER_OPEN",
            reason="unconfirmed open", event_id="paper-open-unconfirmed",
            actor="risk_manager", evidence_version="risk-evidence-v1", now=now,
        )

    opened = store.transition(
        record.opportunity_id, expected_version=5, to_state="PAPER_OPEN",
        reason="paper ledger confirms simulated position", event_id="paper-open-confirmed",
        actor="paper_ledger", evidence_version="paper-position-123", now=now,
    )
    assert opened.opportunity.state == "PAPER_OPEN"
    assert opened.opportunity.orders_authorized is False
