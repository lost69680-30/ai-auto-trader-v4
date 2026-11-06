from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import uuid

import pytest

from backend.strategy_research_memory import (
    PostgresStrategyResearchMemory,
    StrategyResearchExperiment,
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


def _repo_root():
    for parent in Path(__file__).resolve().parents:
        if (parent / "supabase" / "migrations").is_dir():
            return parent
    raise RuntimeError("repository migration directory was not found")


def _record(*, experiment_version=1, orders_authorized=False, candidate_fingerprint=None):
    end = datetime.now(timezone.utc)
    return StrategyResearchExperiment(
        experiment_id="research-" + uuid.uuid4().hex,
        candidate_id="external-candidate-alpha",
        candidate_fingerprint=candidate_fingerprint or ("candidate-fingerprint-" + uuid.uuid4().hex),
        source_provenance_id="source-provenance-alpha",
        experiment_version=experiment_version,
        dataset_start=end - timedelta(days=180),
        dataset_end=end - timedelta(days=1),
        configuration_version="research-config-v1",
        assumptions={
            "fee_bps_per_side": 4.0,
            "slippage_bps_per_side": 2.0,
            "cost_labels": ("fee", "slippage"),
            "orders_authorized": False,
        },
        outcomes={
            "oos_trade_count": 42,
            "oos_net_return_pct": 3.2,
            "max_drawdown_pct": 4.1,
            "orders_authorized": False,
        },
        status="EVALUATED",
        failure_reasons=(),
        orders_authorized=orders_authorized,
    )


def _apply_migration():
    import psycopg
    root = _repo_root()
    migrations = (
        root / "supabase/migrations/20261029205318_create_paper_ledger_schema.sql",
        root / "supabase/migrations/20261105173521_create_strategy_research_memory.sql",
        root / "supabase/migrations/20261105173604_harden_strategy_research_trigger_search_path.sql",
    )
    with psycopg.connect(DSN, autocommit=True) as conn:
        # The disposable integration database mirrors the Supabase client roles
        # so the migration's explicit privilege revocations are testable.
        for role_name in ("anon", "authenticated", "service_role"):
            exists = conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,),
            ).fetchone()
            if exists is None:
                conn.execute(f"CREATE ROLE {role_name} NOLOGIN")
        for migration in migrations:
            conn.execute(migration.read_text(encoding="utf-8"), prepare=False)


def test_memory_is_durable_append_only_and_idempotent_across_store_instances():
    _apply_migration()
    first_store = PostgresStrategyResearchMemory(DSN)
    record = _record()
    first = first_store.append(record, idempotency_key="external-alpha-v1")
    second_store = PostgresStrategyResearchMemory(DSN)
    duplicate = second_store.append(record, idempotency_key="external-alpha-v1")

    assert first.experiment == record
    assert first.duplicate is False
    assert duplicate.experiment == record
    assert duplicate.duplicate is True
    assert second_store.list_by_candidate(record.candidate_fingerprint) == (record,)


def test_memory_rejects_idempotency_conflicts_and_duplicate_versions():
    _apply_migration()
    store = PostgresStrategyResearchMemory(DSN)
    fingerprint = "candidate-fingerprint-conflict"
    first = _record(candidate_fingerprint=fingerprint)
    store.append(first, idempotency_key="memory-conflict-key")
    conflicting = _record(experiment_version=2, candidate_fingerprint=fingerprint)

    with pytest.raises(ValueError, match="idempotency"):
        store.append(conflicting, idempotency_key="memory-conflict-key")

    with pytest.raises(ValueError, match="version"):
        store.append(
            _record(experiment_version=1, candidate_fingerprint=fingerprint),
            idempotency_key="memory-different-key",
        )


def test_memory_rejects_authorized_or_invalid_experiment_data():
    with pytest.raises(ValueError, match="orders_authorized"):
        _record(orders_authorized=True)
    end = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="dataset"):
        StrategyResearchExperiment(
            experiment_id="bad-window",
            candidate_id="candidate",
            candidate_fingerprint="fingerprint",
            source_provenance_id="source",
            experiment_version=1,
            dataset_start=end,
            dataset_end=end - timedelta(days=1),
            configuration_version="v1",
            assumptions={"orders_authorized": False},
            outcomes={"orders_authorized": False},
            status="REJECTED",
            failure_reasons=("invalid sample",),
            orders_authorized=False,
        )


def test_database_enforces_append_only_research_history():
    import psycopg

    _apply_migration()
    record = _record()
    PostgresStrategyResearchMemory(DSN).append(
        record, idempotency_key="append-only-" + uuid.uuid4().hex,
    )
    with psycopg.connect(DSN, autocommit=True) as conn:
        with pytest.raises(psycopg.Error, match="append-only"):
            conn.execute(
                """UPDATE paper_ledger.strategy_research_experiments
                   SET status = 'REJECTED' WHERE experiment_id = %s""",
                (record.experiment_id,),
            )


def test_research_memory_is_rls_enabled_and_private_to_backend_owner():
    import psycopg

    _apply_migration()
    with psycopg.connect(DSN) as conn:
        current_user, table_owner, rls_enabled, force_rls = conn.execute(
            """
            SELECT current_user,
                   pg_get_userbyid(c.relowner),
                   c.relrowsecurity,
                   c.relforcerowsecurity
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = 'paper_ledger'
              AND c.relname = 'strategy_research_experiments'
            """
        ).fetchone()
        assert current_user == table_owner
        assert rls_enabled is True
        assert force_rls is False
        assert conn.execute(
            "SELECT has_table_privilege(current_user, 'paper_ledger.strategy_research_experiments', 'SELECT')"
        ).fetchone()[0] is True
        assert conn.execute(
            "SELECT has_table_privilege(current_user, 'paper_ledger.strategy_research_experiments', 'INSERT')"
        ).fetchone()[0] is True
        for role_name in ("anon", "authenticated", "service_role"):
            assert conn.execute(
                "SELECT has_table_privilege(%s, 'paper_ledger.strategy_research_experiments', 'SELECT')",
                (role_name,),
            ).fetchone()[0] is False
            assert conn.execute(
                "SELECT has_table_privilege(%s, 'paper_ledger.strategy_research_experiments', 'INSERT')",
                (role_name,),
            ).fetchone()[0] is False
