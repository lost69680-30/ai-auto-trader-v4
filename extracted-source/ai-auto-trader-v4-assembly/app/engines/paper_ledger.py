"""Durable SQLite paper-trading ledger; intentionally has no exchange adapter.

Only local simulated orders, fills, and positions are handled here. This module
never sends network requests or authorizes real exchange orders.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Literal, Mapping

from app.engines.orchestrator import PaperOrderIntent


LedgerStatus = Literal["RECORDED", "DUPLICATE", "CONFLICT", "BLOCKED_BY_EMERGENCY_STOP"]
ExecutionStatus = Literal[
    "FILLED", "ALREADY_FILLED", "FILL_CONFLICT", "NOT_FOUND", "ORDER_NOT_PENDING",
    "FILL_BLOCKED_BY_PROTECTION", "EMERGENCY_STOP_ACTIVE", "ORDER_CANCELLED"
]
CloseStatus = Literal[
    "CLOSED", "ALREADY_CLOSED", "CLOSE_CONFLICT", "POSITION_NOT_FOUND", "POSITION_NOT_OPEN"
]


@dataclass(frozen=True, slots=True)
class PaperOrderRecord:
    proposal_id: str
    signal_id: str
    symbol: str
    side: str
    quantity: float
    entry_price: float
    stop_loss: float
    take_profit: float
    recorded_at: float
    status: str


@dataclass(frozen=True, slots=True)
class PaperFill:
    fill_id: int
    proposal_id: str
    symbol: str
    side: str
    quantity: float
    market_price: float
    price: float
    fee: float
    fee_rate: float
    slippage_bps: float
    filled_at: float


@dataclass(frozen=True, slots=True)
class PaperPosition:
    proposal_id: str
    symbol: str
    side: str
    quantity: float
    entry_price: float
    mark_price: float
    stop_loss: float
    take_profit: float
    opened_at: float
    mark_observed_at: float
    status: str
    opening_fee: float
    closing_fee: float
    funding_pnl: float
    realized_pnl: float | None
    closed_at: float | None
    close_price: float | None

    @property
    def unrealized_pnl(self) -> float:
        if self.status != "OPEN":
            return 0.0
        if self.side == "LONG":
            return (self.mark_price - self.entry_price) * self.quantity
        return (self.entry_price - self.mark_price) * self.quantity

    @property
    def total_fees(self) -> float:
        return self.opening_fee + self.closing_fee

    @property
    def total_pnl(self) -> float:
        """Net P&L including realized/unrealized price movement, fees, and funding."""
        if self.status == "CLOSED":
            return float(self.realized_pnl or 0.0)
        return self.unrealized_pnl - self.opening_fee + self.funding_pnl


@dataclass(frozen=True, slots=True)
class PaperExecutionResult:
    status: ExecutionStatus
    reason: str
    fill: PaperFill | None = None
    position: PaperPosition | None = None
    orders_authorized: bool = False


@dataclass(frozen=True, slots=True)
class PaperCloseResult:
    status: CloseStatus
    reason: str
    position: PaperPosition | None = None
    orders_authorized: bool = False


@dataclass(frozen=True, slots=True)
class PaperAuditEvent:
    event_id: int
    event_type: str
    proposal_id: str | None
    occurred_at: float
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class RealizedRiskReport:
    """Ledger-derived realized account risk; values are paper-account estimates."""
    as_of: float
    utc_day_start: float
    daily_realized_pnl: float
    daily_loss: float
    initial_equity: float
    ending_realized_equity: float
    max_drawdown_amount: float
    max_drawdown_pct: float
    realized_event_count: int
    orders_authorized: bool = False


@dataclass(frozen=True, slots=True)
class MarkToMarketRiskReport:
    """Durable paper-account equity snapshot and drawdown state."""
    as_of: float
    account_equity: float
    initial_equity: float
    high_water_equity: float
    drawdown_amount: float
    drawdown_pct: float
    unrealized_pnl: float
    open_position_count: int
    oldest_mark_age_seconds: float
    marks_fresh: bool
    orders_authorized: bool = False


class SQLitePaperLedger:
    """Persistent, idempotent paper ledger using a local SQLite database.

    A file-backed database persists across process restarts. The API only creates
    simulated fills and positions; it has no exchange credentials or order API.
    Fee and slippage defaults are illustrative inputs, not exchange-specific claims.
    """

    @property
    def orders_authorized(self) -> bool:
        """Hard-coded false: this ledger cannot authorize exchange orders."""
        return False

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        if not self.database_path.strip():
            raise ValueError("A file path is required for the persistent paper ledger.")
        if self.database_path != ":memory:":
            resolved_path = Path(self.database_path).expanduser()
            resolved_path.parent.mkdir(parents=True, exist_ok=True)
            self.database_path = str(resolved_path)
        self._lock = RLock()
        self._connection = sqlite3.connect(
            self.database_path,
            timeout=10.0,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 10000")
            if self.database_path != ":memory:":
                self._connection.execute("PRAGMA journal_mode = WAL")
            self._create_schema()
        except Exception:
            # Do not leak a half-initialized connection when startup detects a
            # damaged or incompatible database. Callers must fail closed.
            self._connection.close()
            raise

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_orders (
                proposal_id TEXT PRIMARY KEY,
                signal_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('LONG', 'SHORT')),
                quantity REAL NOT NULL CHECK(quantity > 0),
                entry_price REAL NOT NULL CHECK(entry_price > 0),
                stop_loss REAL NOT NULL CHECK(stop_loss > 0),
                take_profit REAL NOT NULL CHECK(take_profit > 0),
                recorded_at REAL NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('PENDING_PAPER_SIMULATION', 'FILLED', 'CLOSED'))
            );
            CREATE TABLE IF NOT EXISTS paper_fills (
                fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id TEXT NOT NULL UNIQUE REFERENCES paper_orders(proposal_id),
                symbol TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('LONG', 'SHORT')),
                quantity REAL NOT NULL CHECK(quantity > 0),
                market_price REAL NOT NULL CHECK(market_price > 0),
                fill_price REAL NOT NULL CHECK(fill_price > 0),
                fee REAL NOT NULL CHECK(fee >= 0),
                fee_rate REAL NOT NULL CHECK(fee_rate >= 0),
                slippage_bps REAL NOT NULL CHECK(slippage_bps >= 0),
                filled_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_positions (
                proposal_id TEXT PRIMARY KEY REFERENCES paper_orders(proposal_id),
                symbol TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('LONG', 'SHORT')),
                quantity REAL NOT NULL CHECK(quantity > 0),
                entry_price REAL NOT NULL CHECK(entry_price > 0),
                mark_price REAL NOT NULL CHECK(mark_price > 0),
                stop_loss REAL NOT NULL CHECK(stop_loss > 0),
                take_profit REAL NOT NULL CHECK(take_profit > 0),
                opened_at REAL NOT NULL,
                mark_observed_at REAL NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('OPEN', 'CLOSED')),
                opening_fee REAL NOT NULL CHECK(opening_fee >= 0),
                closing_fee REAL NOT NULL DEFAULT 0 CHECK(closing_fee >= 0),
                funding_pnl REAL NOT NULL DEFAULT 0,
                realized_pnl REAL,
                closed_at REAL,
                close_price REAL,
                close_market_price REAL,
                close_fee_rate REAL,
                close_slippage_bps REAL
            );
            CREATE TABLE IF NOT EXISTS paper_funding_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                funding_id TEXT NOT NULL UNIQUE,
                proposal_id TEXT NOT NULL REFERENCES paper_positions(proposal_id),
                symbol TEXT NOT NULL,
                funding_rate REAL NOT NULL,
                mark_price REAL NOT NULL CHECK(mark_price > 0),
                amount REAL NOT NULL,
                occurred_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_paper_funding_proposal ON paper_funding_events(proposal_id, occurred_at);
            CREATE TABLE IF NOT EXISTS paper_audit_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                proposal_id TEXT,
                occurred_at REAL NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_system_controls (
                control_key TEXT PRIMARY KEY,
                active INTEGER NOT NULL CHECK(active IN (0, 1)),
                updated_at REAL NOT NULL,
                reason TEXT NOT NULL,
                activated_at REAL
            );
            CREATE TABLE IF NOT EXISTS paper_order_cancellations (
                proposal_id TEXT PRIMARY KEY REFERENCES paper_orders(proposal_id),
                cancelled_at REAL NOT NULL,
                reason TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_position_extrema (
                proposal_id TEXT PRIMARY KEY REFERENCES paper_positions(proposal_id),
                high_water REAL NOT NULL CHECK(high_water > 0),
                low_water REAL NOT NULL CHECK(low_water > 0)
            );
            CREATE TABLE IF NOT EXISTS paper_equity_snapshots (
                snapshot_at REAL PRIMARY KEY CHECK(snapshot_at >= 0),
                account_equity REAL NOT NULL,
                high_water_equity REAL NOT NULL,
                drawdown_amount REAL NOT NULL CHECK(drawdown_amount >= 0),
                drawdown_pct REAL NOT NULL CHECK(drawdown_pct >= 0),
                unrealized_pnl REAL NOT NULL,
                open_position_count INTEGER NOT NULL CHECK(open_position_count >= 0),
                oldest_mark_age_seconds REAL NOT NULL CHECK(oldest_mark_age_seconds >= 0),
                marks_fresh INTEGER NOT NULL CHECK(marks_fresh IN (0, 1))
            );
            CREATE INDEX IF NOT EXISTS idx_paper_orders_status ON paper_orders(status);
            CREATE INDEX IF NOT EXISTS idx_paper_positions_status ON paper_positions(status);
            CREATE INDEX IF NOT EXISTS idx_paper_audit_proposal ON paper_audit_events(proposal_id);
            """
        )
        # Backward-compatible migration for databases created before funding accounting.
        position_columns = {row["name"] for row in self._connection.execute("PRAGMA table_info(paper_positions)")}
        if "funding_pnl" not in position_columns:
            self._connection.execute("ALTER TABLE paper_positions ADD COLUMN funding_pnl REAL NOT NULL DEFAULT 0")
        self._connection.execute(
            """INSERT OR IGNORE INTO paper_system_controls
               (control_key, active, updated_at, reason, activated_at)
               VALUES ('emergency_stop', 0, 0, '', NULL)"""
        )

    def verify_integrity(self) -> str:
        """Verify SQLite pages and foreign-key consistency; raise on damaged state."""
        with self._lock:
            rows = self._connection.execute("PRAGMA integrity_check").fetchall()
            messages = [str(row[0]) for row in rows]
            if messages != ["ok"]:
                detail = "; ".join(messages[:10]) or "no integrity result"
                raise sqlite3.DatabaseError(f"paper ledger integrity check failed: {detail}")
            foreign_key_errors = self._connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_errors:
                raise sqlite3.IntegrityError(
                    f"paper ledger contains {len(foreign_key_errors)} foreign-key violation(s)"
                )
        return "ok"

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SQLitePaperLedger":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    @property
    def orders(self) -> tuple[PaperOrderRecord, ...]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT o.proposal_id, o.signal_id, o.symbol, o.side, o.quantity,
                          o.entry_price, o.stop_loss, o.take_profit, o.recorded_at,
                          CASE WHEN c.proposal_id IS NOT NULL THEN 'CANCELLED' ELSE o.status END AS status
                   FROM paper_orders o
                   LEFT JOIN paper_order_cancellations c ON c.proposal_id = o.proposal_id
                   ORDER BY o.recorded_at, o.proposal_id"""
            ).fetchall()
            return tuple(self._order_from_row(row) for row in rows)

    @property
    def emergency_stop_active(self) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT active FROM paper_system_controls WHERE control_key = 'emergency_stop'"
            ).fetchone()
            return bool(row["active"]) if row else False

    @property
    def emergency_stop_reason(self) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT active, reason FROM paper_system_controls WHERE control_key = 'emergency_stop'"
            ).fetchone()
            return row["reason"] if row and row["active"] else None

    def activate_emergency_stop(self, *, occurred_at: float, reason: str) -> tuple[str, ...]:
        """Persist a fail-closed stop and cancel all not-yet-filled paper intents.

        This operation never invents prices or closes positions. A caller must provide
        fresh market prices separately if it wants to simulate flattening open positions.
        """
        self._ensure_finite("occurred_at", occurred_at)
        if occurred_at < 0:
            raise ValueError("Emergency-stop timestamps must be non-negative.")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("An emergency-stop reason is required.")
        reason = reason.strip()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                state = self._connection.execute(
                    "SELECT active, updated_at FROM paper_system_controls WHERE control_key = 'emergency_stop'"
                ).fetchone()
                if state is None:
                    raise RuntimeError("Emergency-stop control row is missing.")
                if occurred_at < state["updated_at"]:
                    raise ValueError("Emergency-stop time cannot move backwards.")
                if state["active"]:
                    self._connection.execute("ROLLBACK")
                    return ()
                self._connection.execute(
                    """UPDATE paper_system_controls
                       SET active = 1, updated_at = ?, reason = ?, activated_at = ?
                       WHERE control_key = 'emergency_stop'""",
                    (occurred_at, reason, occurred_at),
                )
                pending = self._connection.execute(
                    """SELECT o.proposal_id FROM paper_orders o
                       LEFT JOIN paper_order_cancellations c ON c.proposal_id = o.proposal_id
                       WHERE o.status = 'PENDING_PAPER_SIMULATION' AND c.proposal_id IS NULL
                       ORDER BY o.recorded_at, o.proposal_id"""
                ).fetchall()
                cancelled_ids: list[str] = []
                for item in pending:
                    proposal_id = item["proposal_id"]
                    self._connection.execute(
                        "INSERT INTO paper_order_cancellations(proposal_id, cancelled_at, reason) VALUES (?, ?, ?)",
                        (proposal_id, occurred_at, reason),
                    )
                    self._insert_audit(
                        "PAPER_ORDER_CANCELLED_BY_EMERGENCY_STOP", proposal_id, occurred_at,
                        {"reason": reason},
                    )
                    cancelled_ids.append(proposal_id)
                self._insert_audit(
                    "SYSTEM_EMERGENCY_STOP_ACTIVATED", None, occurred_at,
                    {"reason": reason, "cancelled_order_ids": cancelled_ids},
                )
                self._connection.execute("COMMIT")
                return tuple(cancelled_ids)
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def cancel_pending_paper_orders(self, *, occurred_at: float, reason: str) -> tuple[str, ...]:
        """Durably cancel all pending paper intents when their trigger state is not recoverable.

        Signal watches and price plans are process-local. On a service restart they
        cannot be trusted, so callers should cancel pending intents and require fresh
        market data and a newly validated signal. This only affects simulated intents.
        """
        self._ensure_finite("occurred_at", occurred_at)
        if occurred_at < 0:
            raise ValueError("Cancellation timestamp must be non-negative.")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("A cancellation reason is required.")
        reason = reason.strip()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                pending = self._connection.execute(
                    """SELECT o.proposal_id, o.recorded_at FROM paper_orders o
                       LEFT JOIN paper_order_cancellations c ON c.proposal_id = o.proposal_id
                       WHERE o.status = 'PENDING_PAPER_SIMULATION' AND c.proposal_id IS NULL
                       ORDER BY o.recorded_at, o.proposal_id"""
                ).fetchall()
                cancelled_ids: list[str] = []
                for item in pending:
                    if occurred_at < item["recorded_at"]:
                        raise ValueError("Cancellation time cannot precede a pending paper intent.")
                    proposal_id = item["proposal_id"]
                    self._connection.execute(
                        "INSERT INTO paper_order_cancellations(proposal_id, cancelled_at, reason) VALUES (?, ?, ?)",
                        (proposal_id, occurred_at, reason),
                    )
                    self._insert_audit(
                        "PAPER_ORDER_CANCELLED_ON_SERVICE_START", proposal_id, occurred_at,
                        {"reason": reason, "orders_authorized": False},
                    )
                    cancelled_ids.append(proposal_id)
                if cancelled_ids:
                    self._insert_audit(
                        "PENDING_PAPER_INTENTS_CANCELLED_ON_SERVICE_START", None, occurred_at,
                        {"reason": reason, "cancelled_order_ids": cancelled_ids, "orders_authorized": False},
                    )
                self._connection.execute("COMMIT")
                return tuple(cancelled_ids)
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def clear_emergency_stop(self, *, occurred_at: float, reason: str) -> bool:
        """Manually clear the durable stop; callers should require a human review."""
        self._ensure_finite("occurred_at", occurred_at)
        if occurred_at < 0:
            raise ValueError("Emergency-stop timestamps must be non-negative.")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("A reset reason is required.")
        reason = reason.strip()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                state = self._connection.execute(
                    "SELECT active, updated_at FROM paper_system_controls WHERE control_key = 'emergency_stop'"
                ).fetchone()
                if state is None:
                    raise RuntimeError("Emergency-stop control row is missing.")
                if occurred_at < state["updated_at"]:
                    raise ValueError("Emergency-stop time cannot move backwards.")
                if not state["active"]:
                    self._connection.execute("ROLLBACK")
                    return False
                open_count = self._connection.execute(
                    "SELECT COUNT(*) AS n FROM paper_positions WHERE status = 'OPEN'"
                ).fetchone()["n"]
                if open_count:
                    raise ValueError("Emergency stop cannot be cleared while paper positions remain open.")
                self._connection.execute(
                    """UPDATE paper_system_controls SET active = 0, updated_at = ?, reason = ?
                       WHERE control_key = 'emergency_stop'""",
                    (occurred_at, reason),
                )
                self._insert_audit(
                    "SYSTEM_EMERGENCY_STOP_CLEARED", None, occurred_at,
                    {"reason": reason},
                )
                self._connection.execute("COMMIT")
                return True
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    @property
    def fills(self) -> tuple[PaperFill, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM paper_fills ORDER BY filled_at, fill_id"
            ).fetchall()
            return tuple(self._fill_from_row(row) for row in rows)

    @property
    def positions(self) -> tuple[PaperPosition, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM paper_positions ORDER BY opened_at, proposal_id"
            ).fetchall()
            return tuple(self._position_from_row(row) for row in rows)

    @property
    def open_positions(self) -> tuple[PaperPosition, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM paper_positions WHERE status = 'OPEN' ORDER BY opened_at, proposal_id"
            ).fetchall()
            return tuple(self._position_from_row(row) for row in rows)

    def realized_risk_report(self, *, initial_equity: float, as_of: float) -> RealizedRiskReport:
        """Calculate daily realized P&L and realized-equity drawdown from durable ledger rows.

        UTC calendar day is used for daily aggregation. Closed-position P&L is split
        into the non-funding close result and individual funding cash-flow events so
        funding is attributed to its actual event time exactly once. Unrealized P&L
        is intentionally excluded: this is a realized-risk report, not total account
        equity or a substitute for a separate mark-to-market exposure gate.
        """
        self._ensure_finite("initial_equity", initial_equity)
        self._ensure_finite("as_of", as_of)
        if initial_equity <= 0:
            raise ValueError("initial_equity must be positive")
        if as_of < 0:
            raise ValueError("as_of must be non-negative")
        import datetime
        utc_day_start = datetime.datetime.fromtimestamp(as_of, datetime.timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        with self._lock:
            closed = self._connection.execute(
                "SELECT proposal_id, realized_pnl, funding_pnl, closed_at FROM paper_positions "
                "WHERE status = 'CLOSED' AND closed_at IS NOT NULL AND closed_at <= ?",
                (as_of,),
            ).fetchall()
            funding = self._connection.execute(
                "SELECT proposal_id, amount, occurred_at FROM paper_funding_events WHERE occurred_at <= ?",
                (as_of,),
            ).fetchall()
        funding_by_position: dict[str, float] = {}
        events: list[tuple[float, str, float]] = []
        for row in funding:
            pid = str(row["proposal_id"])
            amount = float(row["amount"])
            funding_by_position[pid] = funding_by_position.get(pid, 0.0) + amount
            events.append((float(row["occurred_at"]), f"funding:{pid}", amount))
        closed_ids = set()
        for row in closed:
            pid = str(row["proposal_id"])
            closed_ids.add(pid)
            # The close result includes all funding for this position; subtract it
            # here because funding cash flows are added individually by event time.
            close_component = float(row["realized_pnl"] or 0.0) - float(row["funding_pnl"] or 0.0)
            events.append((float(row["closed_at"]), f"close:{pid}", close_component))
        # Funding on still-open positions remains a realized cash flow. Funding for
        # closed positions is also represented above as individual dated events.
        daily_pnl = sum(amount for ts, _, amount in events if utc_day_start <= ts <= as_of)
        equity = float(initial_equity)
        peak = equity
        max_drawdown = 0.0
        for _, _, amount in sorted(events, key=lambda event: (event[0], event[1])):
            equity += amount
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)
        ending_equity = float(initial_equity) + sum(amount for _, _, amount in events)
        return RealizedRiskReport(
            as_of=float(as_of), utc_day_start=utc_day_start,
            daily_realized_pnl=round(daily_pnl, 10), daily_loss=round(max(0.0, -daily_pnl), 10),
            initial_equity=float(initial_equity), ending_realized_equity=round(ending_equity, 10),
            max_drawdown_amount=round(max_drawdown, 10),
            max_drawdown_pct=round((max_drawdown / initial_equity) * 100.0, 10),
            realized_event_count=len(events), orders_authorized=False,
        )

    def mark_to_market_risk_report(
        self, *, initial_equity: float, as_of: float, max_mark_age_seconds: float = 30.0
    ) -> MarkToMarketRiskReport:
        """Persist account equity from ledger P&L and open-position marks.

        A missing/stale/future mark is reported as unhealthy; callers must block new
        risk. Snapshot timestamps may not move backwards. Same-time retries are
        idempotent only when all calculated snapshot values match.
        """
        self._ensure_finite("initial_equity", initial_equity)
        self._ensure_finite("as_of", as_of)
        self._ensure_finite("max_mark_age_seconds", max_mark_age_seconds)
        if initial_equity <= 0 or as_of < 0 or max_mark_age_seconds <= 0:
            raise ValueError("Equity baseline, snapshot time, and mark age limit are invalid.")
        realized = self.realized_risk_report(initial_equity=initial_equity, as_of=as_of)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM paper_positions WHERE status = 'OPEN' ORDER BY proposal_id"
            ).fetchall()
            prior = self._connection.execute(
                "SELECT * FROM paper_equity_snapshots ORDER BY snapshot_at DESC LIMIT 1"
            ).fetchone()
        positions = tuple(self._position_from_row(row) for row in rows)
        unrealized = sum(pos.unrealized_pnl - pos.opening_fee for pos in positions)
        ages = [as_of - pos.mark_observed_at for pos in positions]
        # Age is a duration and must remain non-negative in persisted snapshots.
        # A future-dated mark is still unhealthy (marks_fresh=False), but report
        # its age as zero rather than storing an invalid negative duration.
        oldest_age = max(0.0, max(ages, default=0.0))
        marks_fresh = all(0 <= age <= max_mark_age_seconds for age in ages)
        if not math.isfinite(unrealized):
            raise ValueError("Unrealized P&L is non-finite.")
        account_equity = realized.ending_realized_equity + unrealized
        previous_high = float(prior["high_water_equity"]) if prior else float(initial_equity)
        high_water = max(previous_high, account_equity)
        drawdown = max(0.0, high_water - account_equity)
        drawdown_pct = drawdown / high_water * 100.0 if high_water > 0 else 100.0
        values = (account_equity, high_water, drawdown, drawdown_pct, unrealized, oldest_age)
        if not all(math.isfinite(v) for v in values):
            raise ValueError("Calculated equity snapshot contains non-finite values.")
        snapshot = MarkToMarketRiskReport(
            as_of=float(as_of), account_equity=round(account_equity, 10),
            initial_equity=float(initial_equity), high_water_equity=round(high_water, 10),
            drawdown_amount=round(drawdown, 10), drawdown_pct=round(drawdown_pct, 10),
            unrealized_pnl=round(unrealized, 10), open_position_count=len(positions),
            oldest_mark_age_seconds=round(oldest_age, 10), marks_fresh=marks_fresh,
            orders_authorized=False,
        )
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                latest = self._connection.execute(
                    "SELECT * FROM paper_equity_snapshots ORDER BY snapshot_at DESC LIMIT 1"
                ).fetchone()
                if latest and as_of < float(latest["snapshot_at"]):
                    raise ValueError("Equity snapshot timestamp moved backwards.")
                if latest and as_of == float(latest["snapshot_at"]):
                    same = (
                        abs(float(latest["account_equity"]) - snapshot.account_equity) < 1e-8
                        and abs(float(latest["high_water_equity"]) - snapshot.high_water_equity) < 1e-8
                        and bool(latest["marks_fresh"]) == snapshot.marks_fresh
                    )
                    if not same:
                        raise ValueError("Conflicting equity snapshot for an existing timestamp.")
                else:
                    self._connection.execute(
                        "INSERT INTO paper_equity_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (snapshot.as_of, snapshot.account_equity, snapshot.high_water_equity,
                         snapshot.drawdown_amount, snapshot.drawdown_pct, snapshot.unrealized_pnl,
                         snapshot.open_position_count, snapshot.oldest_mark_age_seconds, int(snapshot.marks_fresh)),
                    )
                    self._insert_audit(
                        "PAPER_EQUITY_SNAPSHOT", None, as_of,
                        {"account_equity": snapshot.account_equity, "high_water_equity": snapshot.high_water_equity,
                         "drawdown_pct": snapshot.drawdown_pct, "marks_fresh": snapshot.marks_fresh,
                         "open_position_count": snapshot.open_position_count},
                    )
                self._connection.execute("COMMIT")
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
        return snapshot

    @property
    def audit_events(self) -> tuple[PaperAuditEvent, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM paper_audit_events ORDER BY event_id"
            ).fetchall()
            return tuple(
                PaperAuditEvent(
                    event_id=row["event_id"],
                    event_type=row["event_type"],
                    proposal_id=row["proposal_id"],
                    occurred_at=row["occurred_at"],
                    payload=json.loads(row["payload_json"]),
                )
                for row in rows
            )

    def record_intent(self, intent: PaperOrderIntent) -> LedgerStatus:
        self._validate_intent(intent)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    "SELECT * FROM paper_orders WHERE proposal_id = ?", (intent.proposal_id,)
                ).fetchone()
                if existing is not None:
                    return_status: LedgerStatus = (
                        "DUPLICATE" if self._same_intent(existing, intent) else "CONFLICT"
                    )
                    self._connection.execute("ROLLBACK")
                    return return_status
                emergency_state = self._connection.execute(
                    "SELECT active, reason FROM paper_system_controls WHERE control_key = 'emergency_stop'"
                ).fetchone()
                if emergency_state and emergency_state["active"]:
                    self._insert_audit(
                        "PAPER_INTENT_BLOCKED_BY_EMERGENCY_STOP", intent.proposal_id,
                        max(intent.recorded_at, self._connection.execute(
                            "SELECT updated_at FROM paper_system_controls WHERE control_key = 'emergency_stop'"
                        ).fetchone()["updated_at"]),
                        {"reason": emergency_state["reason"]},
                    )
                    self._connection.execute("COMMIT")
                    return "BLOCKED_BY_EMERGENCY_STOP"
                self._connection.execute(
                    """INSERT INTO paper_orders
                    (proposal_id, signal_id, symbol, side, quantity, entry_price,
                     stop_loss, take_profit, recorded_at, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_PAPER_SIMULATION')""",
                    (
                        intent.proposal_id,
                        intent.signal_id,
                        intent.symbol.upper(),
                        intent.side,
                        intent.quantity,
                        intent.entry_price,
                        intent.stop_loss,
                        intent.take_profit,
                        intent.recorded_at,
                    ),
                )
                self._insert_audit(
                    "PAPER_ORDER_INTENT_RECORDED",
                    intent.proposal_id,
                    intent.recorded_at,
                    {"symbol": intent.symbol.upper(), "side": intent.side, "quantity": intent.quantity},
                )
                self._connection.execute("COMMIT")
                return "RECORDED"
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def simulate_fill(
        self,
        proposal_id: str,
        *,
        market_price: float,
        filled_at: float,
        fee_rate: float = 0.0004,
        slippage_bps: float = 2.0,
    ) -> PaperExecutionResult:
        self._validate_execution_inputs(market_price, filled_at, fee_rate, slippage_bps)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                emergency_state = self._connection.execute(
                    "SELECT active FROM paper_system_controls WHERE control_key = 'emergency_stop'"
                ).fetchone()
                if emergency_state and emergency_state["active"]:
                    self._connection.execute("ROLLBACK")
                    return PaperExecutionResult(
                        "EMERGENCY_STOP_ACTIVE",
                        "The durable emergency stop is active; paper fills are blocked.",
                    )
                order = self._connection.execute(
                    "SELECT * FROM paper_orders WHERE proposal_id = ?", (proposal_id,)
                ).fetchone()
                if order is None:
                    self._connection.execute("ROLLBACK")
                    return PaperExecutionResult("NOT_FOUND", "No paper intent exists for this proposal.")
                cancellation = self._connection.execute(
                    "SELECT reason FROM paper_order_cancellations WHERE proposal_id = ?", (proposal_id,)
                ).fetchone()
                if cancellation is not None:
                    self._connection.execute("ROLLBACK")
                    return PaperExecutionResult(
                        "ORDER_CANCELLED",
                        "This paper intent was cancelled and cannot be reactivated by clearing the emergency stop.",
                    )
                if filled_at < order["recorded_at"]:
                    raise ValueError("Fill time cannot be older than the paper order intent.")
                existing = self._connection.execute(
                    "SELECT * FROM paper_fills WHERE proposal_id = ?", (proposal_id,)
                ).fetchone()
                if existing is not None:
                    same_fill = (
                        existing["market_price"] == market_price
                        and existing["filled_at"] == filled_at
                        and existing["fee_rate"] == fee_rate
                        and existing["slippage_bps"] == slippage_bps
                    )
                    self._connection.execute("ROLLBACK")
                    if same_fill:
                        return PaperExecutionResult(
                            "ALREADY_FILLED",
                            "The same simulated fill already exists; no second fill was created.",
                            self._fill_from_row(existing),
                            self._get_position(proposal_id),
                        )
                    return PaperExecutionResult(
                        "FILL_CONFLICT",
                        "This proposal already has a different fill; refusing to rewrite history.",
                    )
                if order["status"] != "PENDING_PAPER_SIMULATION":
                    self._connection.execute("ROLLBACK")
                    return PaperExecutionResult(
                        "ORDER_NOT_PENDING", "Only pending paper intents may be filled."
                    )
                direction = 1 if order["side"] == "LONG" else -1
                fill_price = market_price * (1 + direction * slippage_bps / 10_000)
                protection_geometry_valid = (
                    order["stop_loss"] < fill_price < order["take_profit"]
                    if order["side"] == "LONG"
                    else order["take_profit"] < fill_price < order["stop_loss"]
                )
                if not protection_geometry_valid:
                    self._connection.execute("ROLLBACK")
                    return PaperExecutionResult(
                        "FILL_BLOCKED_BY_PROTECTION",
                        "Simulated fill price is outside the proposal's stop-loss/take-profit geometry; no position was opened.",
                    )
                fee = order["quantity"] * fill_price * fee_rate
                self._connection.execute(
                    """INSERT INTO paper_fills
                    (proposal_id, symbol, side, quantity, market_price, fill_price, fee,
                     fee_rate, slippage_bps, filled_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        proposal_id, order["symbol"], order["side"], order["quantity"],
                        market_price, fill_price, fee, fee_rate, slippage_bps, filled_at,
                    ),
                )
                self._connection.execute(
                    """INSERT INTO paper_positions
                    (proposal_id, symbol, side, quantity, entry_price, mark_price, stop_loss,
                     take_profit, opened_at, mark_observed_at, status, opening_fee)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)""",
                    (
                        proposal_id, order["symbol"], order["side"], order["quantity"],
                        fill_price, fill_price, order["stop_loss"], order["take_profit"],
                        filled_at, filled_at, fee,
                    ),
                )
                self._connection.execute(
                    """INSERT INTO paper_position_extrema(proposal_id, high_water, low_water)
                       VALUES (?, ?, ?)""",
                    (proposal_id, fill_price, fill_price),
                )
                self._connection.execute(
                    "UPDATE paper_orders SET status = 'FILLED' WHERE proposal_id = ?",
                    (proposal_id,),
                )
                self._insert_audit(
                    "PAPER_FILL_SIMULATED", proposal_id, filled_at,
                    {"market_price": market_price, "fill_price": fill_price, "fee": fee},
                )
                self._connection.execute("COMMIT")
                fill = self._get_fill(proposal_id)
                position = self._get_position(proposal_id)
                return PaperExecutionResult(
                    "FILLED", "Simulated fill and open position recorded; no exchange order was sent.",
                    fill, position,
                )
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def record_funding_payment(
        self,
        proposal_id: str,
        *,
        funding_id: str,
        funding_rate: float,
        mark_price: float,
        occurred_at: float,
    ) -> str:
        """Record one simulated perpetual-futures funding cash flow idempotently.

        Positive rates mean longs pay and shorts receive. This is ledger simulation
        only: callers must supply a verified funding event and mark; no exchange is called.
        """
        if not isinstance(funding_id, str) or not funding_id.strip():
            raise ValueError("funding_id must be a non-empty stable event identifier")
        funding_id = funding_id.strip()
        self._ensure_finite("funding_rate", funding_rate)
        self._ensure_positive("mark_price", mark_price)
        self._ensure_finite("occurred_at", occurred_at)
        if occurred_at < 0:
            raise ValueError("occurred_at must be non-negative")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    "SELECT * FROM paper_funding_events WHERE funding_id = ?", (funding_id,)
                ).fetchone()
                if existing is not None:
                    same = (existing["proposal_id"] == proposal_id and
                            existing["funding_rate"] == funding_rate and
                            existing["mark_price"] == mark_price and
                            existing["occurred_at"] == occurred_at)
                    self._connection.execute("ROLLBACK")
                    return "DUPLICATE" if same else "CONFLICT"
                position = self._connection.execute(
                    "SELECT * FROM paper_positions WHERE proposal_id = ?", (proposal_id,)
                ).fetchone()
                if position is None:
                    self._connection.execute("ROLLBACK")
                    return "POSITION_NOT_FOUND"
                if occurred_at < position["opened_at"]:
                    raise ValueError("Funding event cannot predate position opening")
                if position["closed_at"] is not None and occurred_at > position["closed_at"]:
                    raise ValueError("Funding event cannot occur after position close")
                # Positive funding: long pays; short receives. Negative funding reverses it.
                sign = -1.0 if position["side"] == "LONG" else 1.0
                amount = sign * position["quantity"] * mark_price * float(funding_rate)
                self._connection.execute(
                    """INSERT INTO paper_funding_events
                       (funding_id, proposal_id, symbol, funding_rate, mark_price, amount, occurred_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (funding_id, proposal_id, position["symbol"], float(funding_rate),
                     float(mark_price), amount, float(occurred_at)),
                )
                new_funding_pnl = position["funding_pnl"] + amount
                if position["status"] == "CLOSED":
                    self._connection.execute(
                        "UPDATE paper_positions SET funding_pnl = ?, realized_pnl = realized_pnl + ? WHERE proposal_id = ?",
                        (new_funding_pnl, amount, proposal_id),
                    )
                else:
                    self._connection.execute(
                        "UPDATE paper_positions SET funding_pnl = ? WHERE proposal_id = ?",
                        (new_funding_pnl, proposal_id),
                    )
                self._insert_audit(
                    "PAPER_FUNDING_RECORDED", proposal_id, float(occurred_at),
                    {"funding_id": funding_id, "funding_rate": float(funding_rate),
                     "mark_price": float(mark_price), "amount": amount},
                )
                self._connection.execute("COMMIT")
                return "RECORDED"
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    @property
    def funding_events(self) -> tuple[Mapping[str, object], ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM paper_funding_events ORDER BY occurred_at, event_id"
            ).fetchall()
            return tuple(dict(row) for row in rows)

    def mark_positions(
        self, market_prices: Mapping[str, float], *, observed_at: float
    ) -> tuple[PaperPosition, ...]:
        self._ensure_finite("observed_at", observed_at)
        if observed_at < 0:
            raise ValueError("observed_at must be non-negative.")
        normalized_prices: dict[str, float] = {}
        for symbol, price in market_prices.items():
            key = symbol.strip().upper()
            if not key:
                raise ValueError("Market-price symbols cannot be blank.")
            self._ensure_positive("market price", price)
            if key in normalized_prices and normalized_prices[key] != price:
                raise ValueError(f"Conflicting market prices supplied for {key}.")
            normalized_prices[key] = float(price)
        changed: list[str] = []
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                rows = self._connection.execute(
                    "SELECT * FROM paper_positions WHERE status = 'OPEN'"
                ).fetchall()
                for row in rows:
                    symbol = row["symbol"].upper()
                    if symbol not in normalized_prices:
                        continue
                    if observed_at < row["mark_observed_at"]:
                        raise ValueError(f"Mark for {symbol} is older than the last accepted mark.")
                    price = normalized_prices[symbol]
                    self._connection.execute(
                        """UPDATE paper_positions SET mark_price = ?, mark_observed_at = ?
                           WHERE proposal_id = ? AND status = 'OPEN'""",
                        (price, observed_at, row["proposal_id"]),
                    )
                    self._insert_audit(
                        "PAPER_POSITION_MARKED", row["proposal_id"], observed_at,
                        {"symbol": symbol, "mark_price": price},
                    )
                    changed.append(row["proposal_id"])
                self._connection.execute("COMMIT")
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
        return tuple(position for position in (self._get_position(pid) for pid in changed) if position)

    def close_position(
        self,
        proposal_id: str,
        *,
        market_price: float,
        closed_at: float,
        fee_rate: float = 0.0004,
        slippage_bps: float = 2.0,
        close_reason: str = "MANUAL",
    ) -> PaperCloseResult:
        self._validate_execution_inputs(market_price, closed_at, fee_rate, slippage_bps)
        if not isinstance(close_reason, str) or not close_reason.strip():
            raise ValueError("close_reason must be a non-empty string.")
        close_reason = close_reason.strip().upper()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                position = self._connection.execute(
                    "SELECT * FROM paper_positions WHERE proposal_id = ?", (proposal_id,)
                ).fetchone()
                if position is None:
                    self._connection.execute("ROLLBACK")
                    return PaperCloseResult("POSITION_NOT_FOUND", "No paper position exists for this proposal.")
                if closed_at < position["mark_observed_at"]:
                    raise ValueError("Close time cannot be older than the last accepted mark/open time.")
                if position["status"] == "CLOSED":
                    same_close = (
                        position["close_market_price"] == market_price
                        and position["closed_at"] == closed_at
                        and position["close_fee_rate"] == fee_rate
                        and position["close_slippage_bps"] == slippage_bps
                    )
                    self._connection.execute("ROLLBACK")
                    if same_close:
                        return PaperCloseResult(
                            "ALREADY_CLOSED", "The same paper close is already recorded.",
                            self._get_position(proposal_id),
                        )
                    return PaperCloseResult(
                        "CLOSE_CONFLICT", "A different close is already recorded; refusing to rewrite history."
                    )
                if position["status"] != "OPEN":
                    self._connection.execute("ROLLBACK")
                    return PaperCloseResult("POSITION_NOT_OPEN", "Only open paper positions can be closed.")
                # Closing a long sells (adverse slippage lowers price); closing a short buys.
                close_direction = -1 if position["side"] == "LONG" else 1
                close_price = market_price * (1 + close_direction * slippage_bps / 10_000)
                closing_fee = position["quantity"] * close_price * fee_rate
                gross_pnl = (
                    (close_price - position["entry_price"]) * position["quantity"]
                    if position["side"] == "LONG"
                    else (position["entry_price"] - close_price) * position["quantity"]
                )
                net_realized_pnl = gross_pnl - position["opening_fee"] - closing_fee + position["funding_pnl"]
                self._connection.execute(
                    """UPDATE paper_positions SET mark_price = ?, mark_observed_at = ?,
                       status = 'CLOSED', closing_fee = ?, realized_pnl = ?, closed_at = ?,
                       close_price = ?, close_market_price = ?, close_fee_rate = ?, close_slippage_bps = ?
                       WHERE proposal_id = ? AND status = 'OPEN'""",
                    (
                        close_price, closed_at, closing_fee, net_realized_pnl, closed_at,
                        close_price, market_price, fee_rate, slippage_bps, proposal_id,
                    ),
                )
                self._connection.execute(
                    "UPDATE paper_orders SET status = 'CLOSED' WHERE proposal_id = ?", (proposal_id,)
                )
                self._insert_audit(
                    "PAPER_POSITION_CLOSED", proposal_id, closed_at,
                    {"market_price": market_price, "close_price": close_price,
                     "gross_pnl": gross_pnl, "net_realized_pnl": net_realized_pnl,
                     "fees": position["opening_fee"] + closing_fee,
                     "close_reason": close_reason},
                )
                self._connection.execute("COMMIT")
                return PaperCloseResult(
                    "CLOSED", "Paper position closed and realized P&L recorded; no exchange order was sent.",
                    self._get_position(proposal_id),
                )
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def advance_trailing_stop(
        self,
        proposal_id: str,
        *,
        updated_at: float,
        trailing_distance_pct: float,
        activation_profit_pct: float,
    ) -> PaperPosition | None:
        """Persist favorable-price extrema and monotonically tighten a trailing stop.

        Percentages are decimal fractions (0.02 means 2%). The current mark must
        already have been persisted by ``mark_positions`` at ``updated_at``.
        """
        self._ensure_finite("updated_at", updated_at)
        self._ensure_finite("trailing_distance_pct", trailing_distance_pct)
        self._ensure_finite("activation_profit_pct", activation_profit_pct)
        if updated_at < 0:
            raise ValueError("updated_at must be non-negative.")
        if not 0 < trailing_distance_pct < 1:
            raise ValueError("trailing_distance_pct must be greater than 0 and below 1.")
        if not 0 <= activation_profit_pct < 1:
            raise ValueError("activation_profit_pct must be between 0 and 1.")

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                position = self._connection.execute(
                    "SELECT * FROM paper_positions WHERE proposal_id = ?", (proposal_id,)
                ).fetchone()
                if position is None or position["status"] != "OPEN":
                    self._connection.execute("ROLLBACK")
                    return self._get_position(proposal_id)
                if position["mark_observed_at"] != updated_at:
                    raise ValueError("Trailing-stop update must match the currently persisted mark timestamp.")
                extrema = self._connection.execute(
                    "SELECT * FROM paper_position_extrema WHERE proposal_id = ?", (proposal_id,)
                ).fetchone()
                if extrema is None:
                    # Forward-compatible recovery for pre-extrema databases.
                    high_water = max(position["entry_price"], position["mark_price"])
                    low_water = min(position["entry_price"], position["mark_price"])
                else:
                    high_water = max(extrema["high_water"], position["mark_price"])
                    low_water = min(extrema["low_water"], position["mark_price"])

                new_stop = position["stop_loss"]
                if position["side"] == "LONG":
                    activated = (high_water - position["entry_price"]) / position["entry_price"] >= activation_profit_pct
                    candidate = high_water * (1 - trailing_distance_pct)
                    if activated and candidate > new_stop and candidate < position["mark_price"] and candidate < position["take_profit"]:
                        new_stop = candidate
                else:
                    activated = (position["entry_price"] - low_water) / position["entry_price"] >= activation_profit_pct
                    candidate = low_water * (1 + trailing_distance_pct)
                    if activated and candidate < new_stop and candidate > position["mark_price"] and candidate > position["take_profit"]:
                        new_stop = candidate

                self._connection.execute(
                    """INSERT INTO paper_position_extrema(proposal_id, high_water, low_water)
                       VALUES (?, ?, ?)
                       ON CONFLICT(proposal_id) DO UPDATE SET
                         high_water = excluded.high_water, low_water = excluded.low_water""",
                    (proposal_id, high_water, low_water),
                )
                if new_stop != position["stop_loss"]:
                    self._connection.execute(
                        "UPDATE paper_positions SET stop_loss = ? WHERE proposal_id = ? AND status = 'OPEN'",
                        (new_stop, proposal_id),
                    )
                    self._insert_audit(
                        "PAPER_TRAILING_STOP_UPDATED", proposal_id, updated_at,
                        {"side": position["side"], "previous_stop": position["stop_loss"],
                         "new_stop": new_stop, "high_water": high_water,
                         "low_water": low_water, "trailing_distance_pct": trailing_distance_pct,
                         "activation_profit_pct": activation_profit_pct},
                    )
                self._connection.execute("COMMIT")
                return self._get_position(proposal_id)
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def _insert_audit(self, event_type: str, proposal_id: str | None, occurred_at: float, payload: dict) -> None:
        self._connection.execute(
            "INSERT INTO paper_audit_events(event_type, proposal_id, occurred_at, payload_json) VALUES (?, ?, ?, ?)",
            (event_type, proposal_id, occurred_at, json.dumps(payload, sort_keys=True, allow_nan=False)),
        )

    def _get_fill(self, proposal_id: str) -> PaperFill | None:
        row = self._connection.execute(
            "SELECT * FROM paper_fills WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
        return self._fill_from_row(row) if row else None

    def _get_position(self, proposal_id: str) -> PaperPosition | None:
        row = self._connection.execute(
            "SELECT * FROM paper_positions WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
        return self._position_from_row(row) if row else None

    @staticmethod
    def _same_intent(row: sqlite3.Row, intent: PaperOrderIntent) -> bool:
        return all(
            row[column] == value
            for column, value in (
                ("signal_id", intent.signal_id), ("symbol", intent.symbol.upper()),
                ("side", intent.side), ("quantity", intent.quantity),
                ("entry_price", intent.entry_price), ("stop_loss", intent.stop_loss),
                ("take_profit", intent.take_profit), ("recorded_at", intent.recorded_at),
            )
        )

    @staticmethod
    def _validate_intent(intent: PaperOrderIntent) -> None:
        if not intent.proposal_id.strip() or not intent.signal_id.strip() or not intent.symbol.strip():
            raise ValueError("Proposal id, signal id, and symbol are required.")
        if intent.side not in ("LONG", "SHORT"):
            raise ValueError("Paper order side must be LONG or SHORT.")
        if intent.orders_authorized or intent.status != "PENDING_PAPER_SIMULATION":
            raise ValueError("Only unauthorized pending paper intents may be recorded.")
        for name in ("quantity", "entry_price", "stop_loss", "take_profit"):
            SQLitePaperLedger._ensure_positive(name, getattr(intent, name))
        SQLitePaperLedger._ensure_finite("recorded_at", intent.recorded_at)
        if intent.recorded_at < 0:
            raise ValueError("recorded_at must be non-negative.")
        if intent.side == "LONG" and not (intent.stop_loss < intent.entry_price < intent.take_profit):
            raise ValueError("LONG paper intents require stop_loss < entry_price < take_profit.")
        if intent.side == "SHORT" and not (intent.take_profit < intent.entry_price < intent.stop_loss):
            raise ValueError("SHORT paper intents require take_profit < entry_price < stop_loss.")

    @staticmethod
    def _validate_execution_inputs(
        market_price: float, timestamp: float, fee_rate: float, slippage_bps: float
    ) -> None:
        SQLitePaperLedger._ensure_positive("market_price", market_price)
        SQLitePaperLedger._ensure_finite("timestamp", timestamp)
        if timestamp < 0:
            raise ValueError("Execution timestamps must be non-negative.")
        SQLitePaperLedger._ensure_finite("fee_rate", fee_rate)
        if fee_rate < 0 or fee_rate > 1:
            raise ValueError("fee_rate must be between 0 and 1 inclusive.")
        SQLitePaperLedger._ensure_finite("slippage_bps", slippage_bps)
        if slippage_bps < 0 or slippage_bps >= 10_000:
            raise ValueError("slippage_bps must be at least 0 and below 10000.")

    @staticmethod
    def _ensure_finite(name: str, value: float) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number.")

    @staticmethod
    def _ensure_positive(name: str, value: float) -> None:
        SQLitePaperLedger._ensure_finite(name, value)
        if value <= 0:
            raise ValueError(f"{name} must be greater than zero.")

    @staticmethod
    def _order_from_row(row: sqlite3.Row) -> PaperOrderRecord:
        return PaperOrderRecord(**{field: row[field] for field in PaperOrderRecord.__dataclass_fields__})

    @staticmethod
    def _fill_from_row(row: sqlite3.Row) -> PaperFill:
        return PaperFill(
            fill_id=row["fill_id"], proposal_id=row["proposal_id"], symbol=row["symbol"],
            side=row["side"], quantity=row["quantity"], market_price=row["market_price"],
            price=row["fill_price"], fee=row["fee"], fee_rate=row["fee_rate"],
            slippage_bps=row["slippage_bps"], filled_at=row["filled_at"],
        )

    @staticmethod
    def _position_from_row(row: sqlite3.Row) -> PaperPosition:
        return PaperPosition(
            proposal_id=row["proposal_id"], symbol=row["symbol"], side=row["side"],
            quantity=row["quantity"], entry_price=row["entry_price"], mark_price=row["mark_price"],
            stop_loss=row["stop_loss"], take_profit=row["take_profit"], opened_at=row["opened_at"],
            mark_observed_at=row["mark_observed_at"], status=row["status"],
            opening_fee=row["opening_fee"], closing_fee=row["closing_fee"],
            funding_pnl=row["funding_pnl"] if "funding_pnl" in row.keys() else 0.0,
            realized_pnl=row["realized_pnl"], closed_at=row["closed_at"], close_price=row["close_price"],
        )
