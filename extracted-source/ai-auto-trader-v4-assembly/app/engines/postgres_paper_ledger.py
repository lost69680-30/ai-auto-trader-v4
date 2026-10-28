"""Supabase/PostgreSQL adapter for the existing safety-first paper ledger.

The domain logic remains in SQLitePaperLedger; this compatibility layer maps its
constrained SQL vocabulary to PostgreSQL. Every mutating ledger operation uses
BEGIN IMMEDIATE in the domain layer, translated to a transaction-scoped global
advisory lock so multiple API workers serialize state changes and retain SQLite
ledger idempotency semantics. A configured PostgreSQL database is never replaced
silently with local SQLite when unavailable or when its schema is wrong.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from threading import RLock
from typing import Any, Iterable

try:
    import psycopg
    from psycopg.pq import TransactionStatus
    from psycopg.rows import tuple_row
except ImportError:  # pragma: no cover - covered in the deployment environment
    psycopg = None
    TransactionStatus = None
    tuple_row = None

from app.engines.paper_ledger import SQLitePaperLedger
from app.engines.postgres_ssl import postgres_connection_ssl_options


def _safe_driver_diagnostic(exc: Exception, dsn: str) -> str:
    """Expose useful driver text while redacting credentials and connection URIs."""
    message = str(exc)
    if dsn:
        message = message.replace(dsn, "[database URI redacted]")
    # PostgreSQL driver errors can echo password parameters separately from the DSN.
    message = re.sub(r"(?i)(password\\s*=\\s*)[^\\s,;\\\"']+", r"\\1[redacted]", message)
    # Do not leak URI user-info if a driver returns a transformed connection string.
    message = re.sub(r"(?i)postgres(?:ql)?://[^\\s,;\\\"']+", "[database URI redacted]", message)
    return " ".join(message.split())[:240]

SCHEMA_NAME = "paper_ledger"
_WRITE_LOCK_KEY = 842_710_621

_REQUIRED_COLUMNS: dict[str, set[str]] = {
    "paper_orders": {
        "proposal_id", "signal_id", "symbol", "side", "quantity", "entry_price",
        "stop_loss", "take_profit", "recorded_at", "status",
    },
    "paper_fills": {
        "fill_id", "proposal_id", "symbol", "side", "quantity", "market_price",
        "fill_price", "fee", "fee_rate", "slippage_bps", "filled_at",
    },
    "paper_positions": {
        "proposal_id", "symbol", "side", "quantity", "entry_price", "mark_price",
        "stop_loss", "take_profit", "opened_at", "mark_observed_at", "status",
        "opening_fee", "closing_fee", "funding_pnl", "realized_pnl", "closed_at",
        "close_price", "close_market_price", "close_fee_rate", "close_slippage_bps",
    },
    "paper_funding_events": {
        "event_id", "funding_id", "proposal_id", "symbol", "funding_rate",
        "mark_price", "amount", "occurred_at",
    },
    "paper_audit_events": {"event_id", "event_type", "proposal_id", "occurred_at", "payload_json"},
    "paper_system_controls": {"control_key", "active", "updated_at", "reason", "activated_at"},
    "paper_order_cancellations": {"proposal_id", "cancelled_at", "reason"},
    "paper_position_extrema": {"proposal_id", "high_water", "low_water"},
    "paper_equity_snapshots": {
        "snapshot_at", "account_equity", "high_water_equity", "drawdown_amount",
        "drawdown_pct", "unrealized_pnl", "open_position_count",
        "oldest_mark_age_seconds", "marks_fresh",
    },
}


class _CompatRow(Mapping[str, Any]):
    """Row supporting both sqlite3.Row-style integer and key lookups."""

    def __init__(self, names: tuple[str, ...], values: tuple[Any, ...]) -> None:
        self._values = values
        self._mapping = dict(zip(names, values))

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return self._mapping[key]

    def __iter__(self):
        return iter(self._mapping)

    def __len__(self) -> int:
        return len(self._mapping)

    def keys(self):
        return self._mapping.keys()


class _CompatCursor:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor
        self._names = tuple(column.name for column in (cursor.description or ()))

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def fetchone(self) -> _CompatRow | None:
        row = self._cursor.fetchone()
        if row is None:
            return None
        return _CompatRow(self._names, tuple(row))

    def fetchall(self) -> list[_CompatRow]:
        return [_CompatRow(self._names, tuple(row)) for row in self._cursor.fetchall()]

    def __iter__(self):
        return self

    def __next__(self) -> _CompatRow:
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row


class _PostgresConnection:
    """Translate only the narrow SQLite SQL subset used by paper_ledger.py."""

    def __init__(self, dsn: str) -> None:
        if psycopg is None:
            raise RuntimeError("PostgreSQL ledger requires the pinned psycopg dependency.")
        try:
            # A persistent session is intentional: this adapter sets search_path and
            # relies on transaction-scoped advisory locks. Use Supabase session mode.
            self._raw = psycopg.connect(
                dsn,
                autocommit=True,
                connect_timeout=10,
                prepare_threshold=None,
                row_factory=tuple_row,
                **postgres_connection_ssl_options(dsn),
            )
        except Exception as exc:
            # Driver text is useful for diagnosis only after URI/password redaction.
            diagnostic = _safe_driver_diagnostic(exc, dsn)
            raise RuntimeError(
                f"Unable to connect to the configured PostgreSQL ledger ({type(exc).__name__}): {diagnostic}"
            ) from None
        self.row_factory = None

    @property
    def in_transaction(self) -> bool:
        return self._raw.info.transaction_status != TransactionStatus.IDLE

    def execute(self, statement: str, parameters: Iterable[Any] | None = None) -> _CompatCursor:
        query = statement.strip()
        upper = query.upper().rstrip(";").strip()

        # SQLite connection tuning pragmas are irrelevant to PostgreSQL.
        if re.fullmatch(r"PRAGMA\s+(FOREIGN_KEYS\s*=\s*ON|BUSY_TIMEOUT\s*=\s*\d+|JOURNAL_MODE\s*=\s*WAL)", upper):
            return _CompatCursor(self._raw.execute("SELECT 1 WHERE FALSE"))

        if re.fullmatch(r"PRAGMA\s+TABLE_INFO\s*\(\s*[A-Z_][A-Z0-9_]*\s*\)", upper):
            match = re.search(r"TABLE_INFO\s*\(\s*([A-Z_][A-Z0-9_]*)\s*\)", upper)
            table_name = match.group(1).lower() if match else ""
            if table_name not in _REQUIRED_COLUMNS:
                raise ValueError("Unsupported table_info request.")
            cursor = self._raw.execute(
                "SELECT column_name AS name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = %s ORDER BY ordinal_position",
                (table_name,),
            )
            return _CompatCursor(cursor)

        if upper == "PRAGMA INTEGRITY_CHECK":
            return _CompatCursor(self._raw.execute("SELECT chr(111) || chr(107) AS integrity_check"))
        if upper == "PRAGMA FOREIGN_KEY_CHECK":
            return _CompatCursor(self._raw.execute("SELECT NULL::text WHERE FALSE"))

        if upper == "BEGIN IMMEDIATE":
            self._raw.execute("BEGIN")
            # Match SQLite BEGIN IMMEDIATE write serialization across API workers.
            cursor = self._raw.execute("SELECT pg_advisory_xact_lock(%s)", (_WRITE_LOCK_KEY,))
            return _CompatCursor(cursor)

        ignored_insert = bool(re.search(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", query, re.IGNORECASE))
        if ignored_insert:
            query = re.sub(
                r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", query,
                count=1, flags=re.IGNORECASE,
            )
            query = query.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

        # SQLite placeholders and REAL columns are the remaining dialect differences.
        query = query.replace("?", "%s")
        query = re.sub(
            r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
            "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY",
            query, flags=re.IGNORECASE,
        )
        query = re.sub(r"\bREAL\b", "DOUBLE PRECISION", query, flags=re.IGNORECASE)
        cursor = (
            self._raw.execute(query, tuple(parameters))
            if parameters is not None
            else self._raw.execute(query)
        )
        return _CompatCursor(cursor)

    def executescript(self, script: str) -> _CompatCursor:
        result = _CompatCursor(self._raw.execute("SELECT 1 WHERE FALSE"))
        for statement in script.split(";"):
            if statement.strip():
                result = self.execute(statement)
        return result

    def close(self) -> None:
        self._raw.close()


class PostgresPaperLedger(SQLitePaperLedger):
    """PostgreSQL-backed implementation of the existing paper-ledger contract.

    The Supabase migration must be applied before this class can open. Missing
    tables/columns, failed connections, or integrity failures raise during startup;
    there is no SQLite fallback when PostgreSQL has been explicitly configured.
    """

    def __init__(self, dsn: str) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("PAPER_DATABASE_URL must be a non-empty PostgreSQL connection string.")
        self.database_path = "postgresql://[configured; credentials redacted]"
        self._lock = RLock()
        self._connection = _PostgresConnection(dsn.strip())
        try:
            self._validate_schema()
            # Reuse the domain ledger's idempotent indexes and emergency-stop seed
            # only after validation confirms the migration created every required table.
            SQLitePaperLedger._create_schema(self)
        except Exception as exc:
            self._connection.close()
            if isinstance(exc, RuntimeError) and "PostgreSQL ledger initialization failed" in str(exc):
                raise
            detail = str(exc).replace(dsn.strip(), "[connection URL redacted]")[:240]
            raise RuntimeError(
                f"PostgreSQL ledger initialization failed ({type(exc).__name__}: {detail}); "
                "no local fallback was attempted."
            ) from None

    def _validate_schema(self) -> None:
        schema_row = self._connection.execute(
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name = %s",
            (SCHEMA_NAME,),
        ).fetchone()
        if schema_row is None:
            raise RuntimeError("Required paper_ledger schema is missing; apply the checked-in migration first.")

        self._connection.execute("SET search_path TO paper_ledger")
        table_rows = self._connection.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
        ).fetchall()
        available_tables = {row["table_name"] for row in table_rows}
        missing_tables = sorted(set(_REQUIRED_COLUMNS) - available_tables)
        if missing_tables:
            raise RuntimeError("Required paper ledger tables are missing: " + ", ".join(missing_tables))

        for table_name, required_columns in _REQUIRED_COLUMNS.items():
            column_rows = self._connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = %s",
                (table_name,),
            ).fetchall()
            available_columns = {row["column_name"] for row in column_rows}
            missing_columns = sorted(required_columns - available_columns)
            if missing_columns:
                raise RuntimeError(
                    f"Required columns missing from {table_name}: " + ", ".join(missing_columns)
                )

    def verify_integrity(self) -> str:
        """Verify schema and relational consistency using PostgreSQL queries."""
        with self._lock:
            self._validate_schema()
            orphan_queries = (
                ("paper_fills", "SELECT COUNT(*) AS n FROM paper_fills f LEFT JOIN paper_orders o ON o.proposal_id = f.proposal_id WHERE o.proposal_id IS NULL"),
                ("paper_positions", "SELECT COUNT(*) AS n FROM paper_positions p LEFT JOIN paper_orders o ON o.proposal_id = p.proposal_id WHERE o.proposal_id IS NULL"),
                ("paper_funding_events", "SELECT COUNT(*) AS n FROM paper_funding_events f LEFT JOIN paper_positions p ON p.proposal_id = f.proposal_id WHERE p.proposal_id IS NULL"),
                ("paper_order_cancellations", "SELECT COUNT(*) AS n FROM paper_order_cancellations c LEFT JOIN paper_orders o ON o.proposal_id = c.proposal_id WHERE o.proposal_id IS NULL"),
                ("paper_position_extrema", "SELECT COUNT(*) AS n FROM paper_position_extrema e LEFT JOIN paper_positions p ON p.proposal_id = e.proposal_id WHERE p.proposal_id IS NULL"),
            )
            for _table, query in orphan_queries:
                row = self._connection.execute(query).fetchone()
                if row is None or int(row["n"]) != 0:
                    raise sqlite3.IntegrityError("Paper ledger contains one or more orphaned records.")
        return "ok"
