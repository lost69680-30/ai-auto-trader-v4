from app.engines.paper_ledger import SQLitePaperLedger
from backend.shared_market_worker import persist_equity_snapshot


def test_persist_equity_snapshot_writes_a_fresh_durable_account_snapshot(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper-ledger.sqlite3")
    try:
        snapshot = persist_equity_snapshot(
            ledger,
            initial_equity=1_000.0,
            as_of=1_800_000_000.0,
            max_mark_age_seconds=30.0,
        )

        row = ledger._connection.execute(
            "SELECT snapshot_at, account_equity, open_position_count, marks_fresh "
            "FROM paper_equity_snapshots ORDER BY snapshot_at DESC LIMIT 1"
        ).fetchone()

        assert row is not None
        assert row["snapshot_at"] == 1_800_000_000.0
        assert row["account_equity"] == 1_000.0
        assert row["open_position_count"] == 0
        assert row["marks_fresh"] == 1
        assert snapshot.account_equity == 1_000.0
        assert snapshot.orders_authorized is False
    finally:
        ledger.close()
