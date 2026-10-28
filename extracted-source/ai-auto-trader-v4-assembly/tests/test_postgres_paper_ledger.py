"""Integration tests for the durable PostgreSQL paper ledger.

PAPER_TEST_DATABASE_URL must point to a disposable local PostgreSQL database.
The fixture rejects non-loopback hosts before dropping/recreating its test schema.
"""
from __future__ import annotations

import os
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import psycopg
import pytest

from app.engines.orchestrator import PaperOrderIntent
from app.engines.paper_ledger import SQLitePaperLedger
from app.engines.sqlite_to_postgres_import import import_sqlite_ledger
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


@pytest.fixture(scope="function", autouse=True)
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


def test_fill_funding_trailing_protection_and_close_are_persisted():
    proposal_id = f"lifecycle-{uuid4().hex}"
    funding_id = f"funding-{uuid4().hex}"
    with PostgresPaperLedger(TEST_DSN) as ledger:
        assert ledger.record_intent(_intent(proposal_id)) == "RECORDED"
        fill = ledger.simulate_fill(
            proposal_id,
            market_price=60100.0,
            filled_at=1_760_000_001.0,
            fee_rate=0.0,
            slippage_bps=0.0,
        )
        assert fill.status == "FILLED"
        assert any(position.proposal_id == proposal_id for position in ledger.open_positions)

        marked = ledger.mark_positions({"BTCUSDT": 61000.0}, observed_at=1_760_000_002.0)
        assert any(position.proposal_id == proposal_id for position in marked)

        tightened = ledger.advance_trailing_stop(
            proposal_id,
            updated_at=1_760_000_002.0,
            trailing_distance_pct=0.02,
            activation_profit_pct=0.005,
        )
        assert tightened is not None
        assert tightened.stop_loss > 58000.0

        assert ledger.record_funding_payment(
            proposal_id,
            funding_id=funding_id,
            funding_rate=0.001,
            mark_price=61000.0,
            occurred_at=1_760_000_003.0,
        ) == "RECORDED"
        assert ledger.record_funding_payment(
            proposal_id,
            funding_id=funding_id,
            funding_rate=0.001,
            mark_price=61000.0,
            occurred_at=1_760_000_003.0,
        ) == "DUPLICATE"

        closed = ledger.close_position(
            proposal_id,
            market_price=61500.0,
            closed_at=1_760_000_004.0,
            fee_rate=0.0,
            slippage_bps=0.0,
            close_reason="TEST",
        )
        assert closed.status == "CLOSED"
        assert not any(position.proposal_id == proposal_id for position in ledger.open_positions)
        assert ledger.orders_authorized is False

    with PostgresPaperLedger(TEST_DSN) as ledger:
        assert any(event["funding_id"] == funding_id for event in ledger.funding_events)
        assert any(position.proposal_id == proposal_id and position.status == "CLOSED" for position in ledger.positions)
        assert ledger.verify_integrity() == "ok"


def test_sqlite_import_preserves_ledger_rows_and_emergency_stop(tmp_path):
    source_path = tmp_path / "legacy-paper-ledger.sqlite3"
    open_proposal = f"import-open-{uuid4().hex}"
    cancelled_proposal = f"import-pending-{uuid4().hex}"
    funding_id = f"import-funding-{uuid4().hex}"

    with SQLitePaperLedger(source_path) as source:
        assert source.record_intent(_intent(open_proposal)) == "RECORDED"
        fill = source.simulate_fill(
            open_proposal,
            market_price=60100.0,
            filled_at=1_760_000_001.0,
            fee_rate=0.0,
            slippage_bps=0.0,
        )
        assert fill.status == "FILLED"
        source.mark_positions({"BTCUSDT": 61000.0}, observed_at=1_760_000_002.0)
        assert source.advance_trailing_stop(
            open_proposal,
            updated_at=1_760_000_002.0,
            trailing_distance_pct=0.02,
            activation_profit_pct=0.005,
        ) is not None
        assert source.record_funding_payment(
            open_proposal,
            funding_id=funding_id,
            funding_rate=0.001,
            mark_price=61000.0,
            occurred_at=1_760_000_003.0,
        ) == "RECORDED"
        source.mark_to_market_risk_report(
            initial_equity=10000.0,
            as_of=1_760_000_003.0,
            max_mark_age_seconds=30.0,
        )
        assert source.record_intent(_intent(cancelled_proposal)) == "RECORDED"
        cancelled = source.activate_emergency_stop(
            occurred_at=1_760_000_005.0,
            reason="imported-emergency-stop",
        )
        assert cancelled_proposal in cancelled
        assert source.emergency_stop_active is True

    imported_counts = import_sqlite_ledger(source_path, TEST_DSN)
    assert imported_counts["paper_orders"] == 2
    assert imported_counts["paper_fills"] == 1
    assert imported_counts["paper_positions"] == 1
    assert imported_counts["paper_funding_events"] == 1
    assert imported_counts["paper_audit_events"] >= 5
    assert imported_counts["paper_system_controls"] == 1
    assert imported_counts["paper_order_cancellations"] == 1
    assert imported_counts["paper_position_extrema"] == 1
    assert imported_counts["paper_equity_snapshots"] == 1

    # A new connection verifies recovery from the imported committed state.
    with PostgresPaperLedger(TEST_DSN) as target:
        assert target.verify_integrity() == "ok"
        assert target.orders_authorized is False
        assert target.emergency_stop_active is True
        assert target.emergency_stop_reason == "imported-emergency-stop"
        assert any(order.proposal_id == cancelled_proposal and order.status == "CANCELLED" for order in target.orders)
        assert any(position.proposal_id == open_proposal and position.status == "OPEN" for position in target.open_positions)
        assert any(event["funding_id"] == funding_id for event in target.funding_events)
        assert target._connection.execute(
            "SELECT COUNT(*) AS n FROM paper_equity_snapshots"
        ).fetchone()["n"] == 1


def test_sqlite_import_refuses_nonempty_postgres_destination(tmp_path):
    source_path = tmp_path / "source-paper-ledger.sqlite3"
    with SQLitePaperLedger(source_path) as source:
        assert source.record_intent(_intent("source-before-import")) == "RECORDED"

    with PostgresPaperLedger(TEST_DSN) as target:
        assert target.record_intent(_intent("existing-destination-order")) == "RECORDED"

    with pytest.raises(ValueError, match="Destination paper ledger is not empty"):
        import_sqlite_ledger(source_path, TEST_DSN)

    with PostgresPaperLedger(TEST_DSN) as target:
        ids = {order.proposal_id for order in target.orders}
        assert ids == {"existing-destination-order"}
        assert target.orders_authorized is False
