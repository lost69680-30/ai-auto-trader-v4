"""One-time, fail-closed import of a SQLite paper ledger into PostgreSQL.

This module never calls an exchange. It refuses a non-empty destination, performs
the import in one PostgreSQL transaction, preserves the Emergency Stop row, verifies
row counts, and advances identity sequences before committing. Credentials must be
provided by the caller from a server-side environment secret.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None


TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "paper_orders": (
        "proposal_id", "signal_id", "symbol", "side", "quantity", "entry_price",
        "stop_loss", "take_profit", "recorded_at", "status",
    ),
    "paper_fills": (
        "fill_id", "proposal_id", "symbol", "side", "quantity", "market_price",
        "fill_price", "fee", "fee_rate", "slippage_bps", "filled_at",
    ),
    "paper_positions": (
        "proposal_id", "symbol", "side", "quantity", "entry_price", "mark_price",
        "stop_loss", "take_profit", "opened_at", "mark_observed_at", "status",
        "opening_fee", "closing_fee", "funding_pnl", "realized_pnl", "closed_at",
        "close_price", "close_market_price", "close_fee_rate", "close_slippage_bps",
    ),
    "paper_funding_events": (
        "event_id", "funding_id", "proposal_id", "symbol", "funding_rate",
        "mark_price", "amount", "occurred_at",
    ),
    "paper_audit_events": ("event_id", "event_type", "proposal_id", "occurred_at", "payload_json"),
    "paper_system_controls": ("control_key", "active", "updated_at", "reason", "activated_at"),
    "paper_order_cancellations": ("proposal_id", "cancelled_at", "reason"),
    "paper_position_extrema": ("proposal_id", "high_water", "low_water"),
    "paper_equity_snapshots": (
        "snapshot_at", "account_equity", "high_water_equity", "drawdown_amount",
        "drawdown_pct", "unrealized_pnl", "open_position_count",
        "oldest_mark_age_seconds", "marks_fresh",
    ),
}
TABLE_ORDER = (
    "paper_orders",
    "paper_fills",
    "paper_positions",
    "paper_funding_events",
    "paper_audit_events",
    "paper_system_controls",
    "paper_order_cancellations",
    "paper_position_extrema",
    "paper_equity_snapshots",
)
IDENTITY_COLUMNS = {"paper_fills": "fill_id", "paper_funding_events": "event_id", "paper_audit_events": "event_id"}
SOURCE_ORDER_BY = {
    "paper_orders": "proposal_id",
    "paper_fills": "fill_id",
    "paper_positions": "proposal_id",
    "paper_funding_events": "event_id",
    "paper_audit_events": "event_id",
    "paper_system_controls": "control_key",
    "paper_order_cancellations": "proposal_id",
    "paper_position_extrema": "proposal_id",
    "paper_equity_snapshots": "snapshot_at",
}


def _read_sqlite_source(sqlite_path: str | Path) -> dict[str, list[dict[str, Any]]]:
    source_path = Path(sqlite_path).expanduser().resolve()
    if not source_path.is_file():
        raise ValueError("Source SQLite paper-ledger file does not exist.")
    uri = source_path.as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=10.0)
    except sqlite3.Error:
        raise ValueError("Unable to open the source SQLite paper ledger.") from None

    try:
        result = [str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall()]
        if result != ["ok"]:
            raise ValueError("Source SQLite paper ledger failed its integrity check.")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Source SQLite paper ledger contains foreign-key violations.")

        present = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        missing = sorted(set(TABLE_COLUMNS) - present)
        if missing:
            raise ValueError("Source SQLite ledger is missing required tables: " + ", ".join(missing))

        rows_by_table: dict[str, list[dict[str, Any]]] = {}
        for table in TABLE_ORDER:
            source_columns = tuple(
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            )
            expected_columns = TABLE_COLUMNS[table]
            if table == "paper_positions" and source_columns == tuple(
                col for col in expected_columns if col != "funding_pnl"
            ):
                # Older ledgers predate funding accounting; the SQLite domain migration
                # defines the safe historic default for this one added field as zero.
                legacy_positions = True
            else:
                legacy_positions = False
                if set(source_columns) != set(expected_columns):
                    raise ValueError(f"Source schema for {table} is incompatible with the importer.")

            records: list[dict[str, Any]] = []
            query = f"SELECT * FROM {table} ORDER BY {SOURCE_ORDER_BY[table]}"
            for row in connection.execute(query).fetchall():
                values = dict(zip(source_columns, tuple(row)))
                if legacy_positions:
                    values["funding_pnl"] = 0.0
                records.append({column: values[column] for column in expected_columns})
            rows_by_table[table] = records

        controls = rows_by_table["paper_system_controls"]
        if not controls or not any(row["control_key"] == "emergency_stop" for row in controls):
            raise ValueError("Source ledger does not contain its required Emergency Stop state.")
        if len([row for row in controls if row["control_key"] == "emergency_stop"]) != 1:
            raise ValueError("Source ledger contains duplicate Emergency Stop control rows.")
        return rows_by_table
    finally:
        connection.close()


def import_sqlite_ledger(sqlite_path: str | Path, database_url: str) -> dict[str, int]:
    """Import a file-backed SQLite ledger once. Refuses an occupied destination.

    The target schema must already be created by the reviewed migration. The target
    may contain only the default inactive Emergency Stop seed inserted at startup.
    """
    if psycopg is None:
        raise RuntimeError("PostgreSQL ledger import requires the pinned psycopg dependency.")
    if not isinstance(database_url, str) or not database_url.strip():
        raise ValueError("A PostgreSQL database URL from a server-side secret is required.")
    rows_by_table = _read_sqlite_source(sqlite_path)
    source_counts = {table: len(rows_by_table[table]) for table in TABLE_ORDER}

    try:
        connection = psycopg.connect(
            database_url.strip(),
            autocommit=True,
            connect_timeout=10,
            prepare_threshold=None,
        )
    except Exception as exc:
        raise RuntimeError(f"Unable to connect to the PostgreSQL destination ({type(exc).__name__}); credentials redacted.") from None

    try:
        with connection.transaction():
            connection.execute("SET LOCAL search_path TO paper_ledger")
            target_counts = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in TABLE_ORDER
            }
            controls = connection.execute(
                "SELECT control_key, active, updated_at, reason, activated_at FROM paper_system_controls"
            ).fetchall()

            non_control_data = {table: count for table, count in target_counts.items() if table != "paper_system_controls" and count}
            if non_control_data:
                raise ValueError(
                    "Destination paper ledger is not empty; import aborted without changing it."
                )
            if len(controls) > 1:
                raise ValueError("Destination has multiple system controls; manual reconciliation is required.")
            if controls:
                default = controls[0]
                if (
                    default[0] != "emergency_stop" or int(default[1]) != 0
                    or float(default[2]) != 0.0 or str(default[3]) != "" or default[4] is not None
                ):
                    raise ValueError(
                        "Destination Emergency Stop is not in its default inactive state; manual reconciliation is required."
                    )

            # Remove only the verified default seed; preserve the exact source stop row.
            connection.execute("DELETE FROM paper_system_controls")
            for table in TABLE_ORDER:
                if not rows_by_table[table]:
                    continue
                columns = TABLE_COLUMNS[table]
                column_sql = ", ".join(columns)
                placeholders = ", ".join(["%s"] * len(columns))
                insert_sql = f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})"
                with connection.cursor() as cursor:
                    cursor.executemany(
                        insert_sql,
                        [tuple(record[column] for column in columns) for record in rows_by_table[table]],
                    )

            # Explicit IDs were preserved; move sequences forward before committing.
            for table, identity_column in IDENTITY_COLUMNS.items():
                max_id = connection.execute(
                    f"SELECT MAX({identity_column}) FROM {table}"
                ).fetchone()[0]
                sequence_name = connection.execute(
                    "SELECT pg_get_serial_sequence(%s, %s)",
                    (f"paper_ledger.{table}", identity_column),
                ).fetchone()[0]
                if sequence_name:
                    connection.execute(
                        "SELECT setval(%s::regclass, %s, %s)",
                        (sequence_name, int(max_id) if max_id is not None else 1, max_id is not None),
                    )

            imported_counts = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in TABLE_ORDER
            }
            if imported_counts != source_counts:
                raise RuntimeError("Post-import row counts do not match the source; transaction rolled back.")

            # Verify the exact control state, especially the stop bit and reason.
            target_stop = connection.execute(
                "SELECT control_key, active, updated_at, reason, activated_at "
                "FROM paper_system_controls WHERE control_key = 'emergency_stop'"
            ).fetchone()
            source_stop = next(row for row in rows_by_table["paper_system_controls"] if row["control_key"] == "emergency_stop")
            if target_stop != tuple(source_stop[column] for column in TABLE_COLUMNS["paper_system_controls"]):
                raise RuntimeError("Emergency Stop state differs after import; transaction rolled back.")

        return imported_counts
    except Exception as exc:
        if isinstance(exc, (ValueError, RuntimeError)):
            raise
        detail = str(exc).replace(database_url.strip(), "[connection URL redacted]")[:240]
        raise RuntimeError(
            f"SQLite-to-PostgreSQL import failed ({type(exc).__name__}: {detail}); "
            "transaction was rolled back."
        ) from None
    finally:
        connection.close()


def main() -> int:
    import argparse
    import json
    import os
    import sys

    parser = argparse.ArgumentParser(
        description="One-time import of the existing SQLite paper ledger into an empty PostgreSQL paper_ledger schema."
    )
    parser.add_argument("--sqlite-path", required=True, help="Path to the source SQLite ledger file.")
    parser.add_argument(
        "--database-url-env",
        default="PAPER_DATABASE_URL",
        help="Name of the environment variable holding the PostgreSQL URL (never pass credentials as a CLI argument).",
    )
    args = parser.parse_args()
    database_url = os.environ.get(args.database_url_env, "")
    if not database_url.strip():
        parser.error(f"Environment variable {args.database_url_env} is missing or empty.")

    try:
        counts = import_sqlite_ledger(args.sqlite_path, database_url)
    except Exception as exc:
        print(f"IMPORT FAILED: {type(exc).__name__}: {str(exc)[:240]}", file=sys.stderr)
        return 2
    print(json.dumps({"status": "IMPORTED_AND_VERIFIED", "rows": counts}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
