"""Integration tests for the durable PostgreSQL paper ledger.

PAPER_TEST_DATABASE_URL must point to a disposable local PostgreSQL database.
The fixture rejects non-loopback hosts before dropping/recreating its test schema.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import psycopg
import pytest

from app.engines.orchestrator import PaperOrderIntent
from app.engines.postgres_paper_ledger import PostgresPaperLedger


TEST_DSN = os.environ.get("PAPER_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="PAPER_TEST_DATABASE_URL is not configured")


def _require_local_test_database() -> None:
    host = (urlparse(TEST_DSN).hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "::1", "postgres"}:
        pytest.fail(
            "Refusing to reset the paper_ledger schema unless PAPER_TEST_DATABASE_URL "
            "points to a local disposable test database."
        )


@pytest.fixture(scope="module", autouse=True)
def disposable_postgres_schema():
    _require_local_test_database()
    repo_root = Path(__file__).resolve().parents[3]
    migration_path = repo_root / "supabase" / "migrations" / "20261028010000_create_paper_ledger_schema.sql"
    if not migration_path.is_file():
        pytest.fail(f"Required migration file is missing: {migration_path.name}")
    with psycopg.connect(TEST_DSN, autocommit=True, prepare_threshold=None) as connection:
        connection.execute("DROP SCHEMA IF EXISTS paper_ledger CASCADE")
        connection.execute(migration_path.read_text(encoding="utf-8"), prepare=False)
    yield


def _intent(proposal_id: str) -> PaperOrderIntent:
    return PaperOrderIntent(
        proposal_id=proposal_id,
        signal_id=f"signal-{proposal_id}",
        symbol="BTCUSDT",
        side="LONG",
        quantity=0.01,
        entry_price=60000.0,
        stop_loss=58000.0,
        take_profit=64000.0,
        recorded_at=1_760_000_000.0,
    )


def test_orders_and_emergency_stop_survive_ledger_recreation():
    proposal_id = f"restart-{uuid4().hex}"
    with PostgresPaperLedger(TEST_DSN) as ledger:
        assert ledger.verify_integrity() == "ok"
        assert ledger.orders_authorized is False
        assert ledger.record_intent(_intent(proposal_id)) == "RECORDED"

    # A fresh ledger instance represents a backend process restart.
    with PostgresPaperLedger(TEST_DSN) as ledger:
        assert any(order.proposal_id == proposal_id for order in ledger.orders)
        assert ledger.record_intent(_intent(proposal_id)) == "DUPLICATE"
        ledger.activate_emergency_stop(
            occurred_at=1_800_000_001.0,
            reason="restart-persistence-test",
        )
        assert ledger.emergency_stop_active is True

    with PostgresPaperLedger(TEST_DSN) as ledger:
        assert ledger.emergency_stop_active is True
        assert ledger.emergency_stop_reason == "restart-persistence-test"
        assert ledger.record_intent(_intent(f"blocked-{uuid4().hex}")) == "BLOCKED_BY_EMERGENCY_STOP"
        assert ledger.orders_authorized is False


def test_concurrent_duplicate_intents_are_serialized_and_idempotent():
    proposal_id = f"parallel-{uuid4().hex}"
    intent = _intent(proposal_id)

    def submit(_: int) -> str:
        with PostgresPaperLedger(TEST_DSN) as ledger:
            return ledger.record_intent(intent)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(16)))

    assert results.count("RECORDED") == 1
    assert results.count("DUPLICATE") == 15
    with PostgresPaperLedger(TEST_DSN) as ledger:
        assert sum(order.proposal_id == proposal_id for order in ledger.orders) == 1
        assert ledger.orders_authorized is False
