"""Read-only storage inventory and capacity assessment for Solo.

This module never creates, updates, deletes, archives, or vacuums database data.
The command-line entry point opens PostgreSQL with default_transaction_read_only=on.
"""
from __future__ import annotations

import json
import os
from typing import Any


def assess_capacity(*, used_bytes: int, quota_bytes: int) -> dict[str, Any]:
    """Classify database capacity using conservative operational thresholds."""
    if isinstance(used_bytes, bool) or isinstance(quota_bytes, bool):
        raise ValueError("Storage measurements must be non-negative integers.")
    if not isinstance(used_bytes, int) or not isinstance(quota_bytes, int):
        raise ValueError("Storage measurements must be non-negative integers.")
    if used_bytes < 0 or quota_bytes <= 0:
        raise ValueError("Storage usage must be non-negative and quota must be positive.")

    usage_percent = round((used_bytes / quota_bytes) * 100, 2)
    if used_bytes >= quota_bytes:
        status = "CAPACITY_EXHAUSTED"
        action = "Pause new paper proposals if required records cannot be persisted; alert immediately."
    elif usage_percent >= 95:
        status = "EMERGENCY"
        action = "Stop nonessential ingestion and investigate capacity immediately; preserve ledger and audit data."
    elif usage_percent >= 85:
        status = "CRITICAL"
        action = "Prepare approved capacity or archival remediation; do not delete financial records."
    elif usage_percent >= 70:
        status = "WARNING"
        action = "Review growth and plan capacity before the next threshold."
    else:
        status = "HEALTHY"
        action = "Continue monitoring."
    return {
        "used_bytes": used_bytes,
        "quota_bytes": quota_bytes,
        "usage_percent": usage_percent,
        "status": status,
        "recommended_action": action,
        # This is an advisory guardrail, not an order authorization.
        "allow_new_paper_proposals": used_bytes < quota_bytes,
    }


def build_storage_report(
    *,
    database_bytes: int,
    quota_bytes: int,
    table_rows: list[dict[str, Any]],
    object_rows: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Build a JSON-safe report without changing caller-owned inventory rows."""
    tables = sorted(
        (dict(row) for row in table_rows),
        key=lambda row: int(row.get("total_bytes") or 0),
        reverse=True,
    )
    if object_rows is None:
        object_storage: dict[str, Any] = {
            "status": "NOT_INSPECTED",
            "bucket_count": None,
            "object_count": None,
            "object_bytes": None,
            "buckets": [],
        }
    else:
        buckets = [dict(row) for row in object_rows]
        object_storage = {
            "status": "INSPECTED",
            "bucket_count": len(buckets),
            "object_count": sum(int(row.get("object_count") or 0) for row in buckets),
            "object_bytes": sum(int(row.get("object_bytes") or 0) for row in buckets),
            "buckets": buckets,
        }

    return {
        "report_version": 1,
        "mode": "READ_ONLY",
        "capacity": assess_capacity(used_bytes=database_bytes, quota_bytes=quota_bytes),
        "largest_tables": tables[:40],
        "object_storage": object_storage,
        "safety": {
            "orders_authorized": False,
            "paper_only": True,
            "mutations_performed": False,
            "financial_records_deleted": False,
        },
    }


_DATABASE_SIZE_SQL = """
SELECT pg_database_size(current_database())::bigint AS database_bytes
"""

_TABLE_SIZE_SQL = """
SELECT
    n.nspname AS schema_name,
    c.relname AS table_name,
    GREATEST(c.reltuples, 0)::bigint AS estimated_rows,
    pg_total_relation_size(c.oid)::bigint AS total_bytes,
    pg_relation_size(c.oid)::bigint AS heap_bytes,
    pg_indexes_size(c.oid)::bigint AS index_bytes
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'm', 'p')
  AND n.nspname IN ('public', 'paper_ledger', 'storage')
ORDER BY pg_total_relation_size(c.oid) DESC
LIMIT 40
"""

_OBJECT_STORAGE_SQL = """
SELECT
    b.id AS bucket_id,
    b.name AS bucket_name,
    b.public AS is_public,
    b.file_size_limit,
    COUNT(o.id)::bigint AS object_count,
    COALESCE(SUM(NULLIF(o.metadata->>'size', '')::bigint), 0)::bigint AS object_bytes
FROM storage.buckets AS b
LEFT JOIN storage.objects AS o ON o.bucket_id = b.id
GROUP BY b.id, b.name, b.public, b.file_size_limit
ORDER BY b.name
"""


def _rows(cursor: Any) -> list[dict[str, Any]]:
    names = [column.name for column in (cursor.description or ())]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def collect_storage_report(connection: Any, *, quota_bytes: int) -> dict[str, Any]:
    """Collect database and object inventory using SELECT statements only."""
    size_row = connection.execute(_DATABASE_SIZE_SQL).fetchone()
    if size_row is None:
        raise RuntimeError("Database size query returned no result.")
    database_bytes = int(size_row[0])
    table_rows = _rows(connection.execute(_TABLE_SIZE_SQL))

    try:
        object_rows = _rows(connection.execute(_OBJECT_STORAGE_SQL))
    except Exception:
        # Lack of Storage schema/table privileges must be visible, not misreported
        # as zero objects. PostgreSQL autocommit is required by the CLI so this
        # failed read cannot leave the connection in an aborted transaction.
        object_rows = None

    return build_storage_report(
        database_bytes=database_bytes,
        quota_bytes=quota_bytes,
        table_rows=table_rows,
        object_rows=object_rows,
    )


def main() -> int:
    database_url = os.environ.get("SOLO_STORAGE_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("Set SOLO_STORAGE_DATABASE_URL to a read-only PostgreSQL connection secret.")

    try:
        quota_bytes = int(os.environ.get("SOLO_DATABASE_QUOTA_BYTES", str(500 * 1024 * 1024)))
    except ValueError:
        raise SystemExit("SOLO_DATABASE_QUOTA_BYTES must be a positive integer.") from None
    if quota_bytes <= 0:
        raise SystemExit("SOLO_DATABASE_QUOTA_BYTES must be a positive integer.")

    try:
        import psycopg
    except ImportError:
        raise SystemExit("Install the project's pinned psycopg dependency before running Storage Guardian.") from None

    # Enforce read-only mode at the PostgreSQL session level. No DDL/DML is used.
    with psycopg.connect(
        database_url,
        autocommit=True,
        connect_timeout=10,
        options="-c default_transaction_read_only=on",
    ) as connection:
        report = collect_storage_report(connection, quota_bytes=quota_bytes)

    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
