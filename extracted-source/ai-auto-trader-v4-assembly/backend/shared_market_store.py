"""PostgreSQL-backed shared market feed for independent read-only worker/API processes."""
from __future__ import annotations

import json
import math
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    import psycopg
except ImportError:  # pragma: no cover - deployment dependency
    psycopg = None

from app.engines.postgres_ssl import postgres_connection_ssl_options


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("market timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _finite(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{field} must be a finite number")
    return float(value)


class PostgresMarketFeedStore:
    """Persist validated poll results and expose them only while worker/data are fresh.

    A database lease prevents two active workers from publishing concurrently.
    This store has no exchange credentials and cannot authorize orders.
    """

    def __init__(
        self, dsn: str, *, worker_id: str = "solo-market-worker",
        owner_id: str | None = None, max_age_seconds: float = 90.0,
        lease_seconds: float = 30.0,
    ) -> None:
        if psycopg is None:
            raise RuntimeError("Shared market feed requires psycopg.")
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("PAPER_DATABASE_URL must be configured for shared market data.")
        if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 120:
            raise ValueError("worker_id must contain 1 to 120 characters")
        if not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        if not math.isfinite(lease_seconds) or lease_seconds < 5:
            raise ValueError("lease_seconds must be at least 5 seconds")
        self.worker_id = worker_id.strip()
        self.owner_id = owner_id or str(uuid.uuid4())
        self.max_age_seconds = float(max_age_seconds)
        self.lease_seconds = float(lease_seconds)
        self._dsn = dsn.strip()

    def _connect(self):
        return psycopg.connect(
            self._dsn, connect_timeout=10, **postgres_connection_ssl_options(self._dsn)
        )

    def acquire_lease(self, *, now: datetime | None = None) -> bool:
        stamp = _iso(now or datetime.now(timezone.utc))
        with self._connect() as conn:
            row = conn.execute(
                """INSERT INTO paper_ledger.market_worker_heartbeats
                   (worker_id, owner_id, started_at, heartbeat_at, lease_expires_at,
                    status, details, orders_authorized)
                   VALUES (%s, %s, %s::timestamptz, %s::timestamptz,
                           %s::timestamptz + (%s * interval '1 second'),
                           'STARTING', '{}'::jsonb, false)
                   ON CONFLICT (worker_id) DO UPDATE SET
                     owner_id = EXCLUDED.owner_id,
                     started_at = EXCLUDED.started_at,
                     heartbeat_at = EXCLUDED.heartbeat_at,
                     lease_expires_at = EXCLUDED.lease_expires_at,
                     status = 'STARTING', details = '{}'::jsonb
                   WHERE paper_ledger.market_worker_heartbeats.lease_expires_at <= %s::timestamptz
                      OR paper_ledger.market_worker_heartbeats.owner_id = EXCLUDED.owner_id
                   RETURNING owner_id""",
                (self.worker_id, self.owner_id, stamp, stamp, stamp,
                 self.lease_seconds, stamp),
            ).fetchone()
            return row is not None and row[0] == self.owner_id

    def publish(self, poll: Any, *, now: datetime | None = None) -> None:
        if getattr(poll, "orders_authorized", None) is not False:
            raise ValueError("market poll cannot authorize orders")
        checked = _iso(poll.checked_at)
        stamp = _iso(now or datetime.now(timezone.utc))
        payloads: list[tuple[str, str | None, str, str]] = []
        for frame in poll.frames:
            if frame.orders_authorized is not False:
                raise ValueError("market frame cannot authorize orders")
            status = getattr(frame.status, "value", str(frame.status))
            if status not in {"HEALTHY", "STALE", "GAP_DETECTED", "DISCONNECTED", "NO_CLOSED_CANDLES", "INVALID_DATA"}:
                raise ValueError("unknown market feed status")
            candles = []
            for candle in frame.closed_candles:
                candles.append({
                    "open_time": _iso(candle.open_time), "close_time": _iso(candle.close_time),
                    "open": _finite(candle.open, "candle.open"),
                    "high": _finite(candle.high, "candle.high"),
                    "low": _finite(candle.low, "candle.low"),
                    "close": _finite(candle.close, "candle.close"),
                    "volume": _finite(candle.volume, "candle.volume"),
                    "is_closed": bool(candle.is_closed),
                })
            futures = frame.market_type == "FUTURES"
            payload = {
                "symbol": frame.symbol, "status": status,
                "mark_price": None if frame.mark_price is None else _finite(frame.mark_price, "mark_price"),
                "mark_observed_at": _iso(frame.mark_observed_at),
                "market_type": frame.market_type,
                "price_type": "FUTURES_MARK_PRICE" if futures else "SPOT_LAST_PRICE",
                "index_price": None if frame.index_price is None else _finite(frame.index_price, "index_price"),
                "funding_rate": None if frame.funding_rate is None else _finite(frame.funding_rate, "funding_rate"),
                "next_funding_at": _iso(frame.next_funding_at),
                "candles": candles[-100:], "reasons": list(frame.reasons),
                "retry_count": int(frame.retry_count), "checked_at": checked,
                "orders_authorized": False,
            }
            encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False)
            payloads.append((frame.symbol, _iso(frame.mark_observed_at), status, encoded))

        with self._connect() as conn:
            lease = conn.execute(
                """SELECT owner_id, lease_expires_at FROM paper_ledger.market_worker_heartbeats
                   WHERE worker_id = %s FOR UPDATE""", (self.worker_id,)
            ).fetchone()
            if (lease is None or lease[0] != self.owner_id
                    or lease[1] <= datetime.now(timezone.utc)):
                raise RuntimeError("market worker lease is missing, expired, or owned by another process")
            active_symbols = [item[0] for item in payloads]
            if active_symbols:
                conn.execute(
                    "DELETE FROM paper_ledger.market_feed_snapshots WHERE worker_id = %s AND NOT (symbol = ANY(%s))",
                    (self.worker_id, active_symbols),
                )
            else:
                conn.execute(
                    "DELETE FROM paper_ledger.market_feed_snapshots WHERE worker_id = %s",
                    (self.worker_id,),
                )
            for symbol, observed, status, encoded in payloads:
                conn.execute(
                    """INSERT INTO paper_ledger.market_feed_snapshots
                       (symbol, worker_id, checked_at, observed_at, feed_status, payload, orders_authorized)
                       VALUES (%s, %s, %s::timestamptz, %s::timestamptz, %s, %s::jsonb, false)
                       ON CONFLICT (symbol) DO UPDATE SET
                         worker_id = EXCLUDED.worker_id, checked_at = EXCLUDED.checked_at,
                         observed_at = EXCLUDED.observed_at, feed_status = EXCLUDED.feed_status,
                         payload = EXCLUDED.payload, updated_at = now()
                       WHERE paper_ledger.market_feed_snapshots.checked_at <= EXCLUDED.checked_at""",
                    (symbol, self.worker_id, checked, observed, status, encoded),
                )
            conn.execute(
                """UPDATE paper_ledger.market_worker_heartbeats
                   SET heartbeat_at = %s::timestamptz,
                       lease_expires_at = %s::timestamptz + (%s * interval '1 second'),
                       status = %s, details = %s::jsonb
                   WHERE worker_id = %s AND owner_id = %s""",
                (stamp, stamp, self.lease_seconds,
                 "RUNNING" if payloads and all(x[2] == "HEALTHY" for x in payloads) else "DEGRADED",
                 json.dumps({"symbols": len(payloads), "orders_authorized": False}),
                 self.worker_id, self.owner_id),
            )

    def heartbeat(self, status: str, *, now: datetime | None = None, details: dict | None = None) -> None:
        if status not in {"STARTING", "RUNNING", "DEGRADED", "STOPPING", "STOPPED", "ERROR"}:
            raise ValueError("invalid worker heartbeat status")
        stamp = _iso(now or datetime.now(timezone.utc))
        with self._connect() as conn:
            row = conn.execute(
                """UPDATE paper_ledger.market_worker_heartbeats
                   SET heartbeat_at = %s::timestamptz,
                       lease_expires_at = %s::timestamptz + (%s * interval '1 second'),
                       status = %s, details = %s::jsonb
                   WHERE worker_id = %s AND owner_id = %s RETURNING worker_id""",
                (stamp, stamp, self.lease_seconds, status,
                 json.dumps(details or {"orders_authorized": False}), self.worker_id, self.owner_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("market worker lease lost")

    def release(self, *, now: datetime | None = None) -> None:
        stamp = _iso(now or datetime.now(timezone.utc))
        with self._connect() as conn:
            conn.execute(
                """UPDATE paper_ledger.market_worker_heartbeats
                   SET heartbeat_at = %s::timestamptz, lease_expires_at = %s::timestamptz,
                       status = 'STOPPED'
                   WHERE worker_id = %s AND owner_id = %s""",
                (stamp, stamp, self.worker_id, self.owner_id),
            )

    def _read(self) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        with self._connect() as conn:
            hb = conn.execute(
                """SELECT worker_id, owner_id, heartbeat_at, lease_expires_at, status
                   FROM paper_ledger.market_worker_heartbeats
                   WHERE worker_id = %s""", (self.worker_id,)
            ).fetchone()
            rows = conn.execute(
                """SELECT payload FROM paper_ledger.market_feed_snapshots
                   WHERE worker_id = %s ORDER BY symbol""", (self.worker_id,)
            ).fetchall()
        return (None if hb is None else {
            "worker_id": hb[0], "owner_id": hb[1], "heartbeat_at": hb[2],
            "lease_expires_at": hb[3], "status": hb[4],
        }), [row[0] for row in rows]

    def status(self) -> dict[str, Any]:
        hb, rows = self._read()
        now = datetime.now(timezone.utc)
        if hb is None:
            return {"status": "STARTING", "reason": "no worker heartbeat", "checked_at": None,
                    "age_seconds": None, "worker_status": "MISSING", "orders_authorized": False}
        age = (now - hb["heartbeat_at"]).total_seconds()
        if hb["status"] == "STARTING":
            return {"status": "STARTING", "reason": "worker has not published its first validated market poll",
                    "checked_at": hb["heartbeat_at"].timestamp(), "age_seconds": age,
                    "worker_status": hb["status"], "orders_authorized": False}
        if age < 0 or age > self.max_age_seconds or hb["lease_expires_at"] <= now or hb["status"] in {"STOPPED", "ERROR"}:
            return {"status": "STALE", "reason": "market worker heartbeat is stale or stopped",
                    "checked_at": hb["heartbeat_at"].timestamp(), "age_seconds": age,
                    "worker_status": hb["status"], "orders_authorized": False}
        if not rows or any((now - datetime.fromisoformat(row["checked_at"])).total_seconds() > self.max_age_seconds for row in rows):
            return {"status": "STALE", "reason": "market snapshots are missing or stale",
                    "checked_at": hb["heartbeat_at"].timestamp(), "age_seconds": age,
                    "worker_status": hb["status"], "orders_authorized": False}
        healthy = sum(1 for row in rows if row["status"] == "HEALTHY")
        feed_status = "HEALTHY" if healthy == len(rows) else ("DEGRADED" if healthy else "OFFLINE")
        return {"status": feed_status, "reason": None if feed_status == "HEALTHY" else "one or more symbols are unhealthy",
                "checked_at": hb["heartbeat_at"].timestamp(), "age_seconds": age,
                "worker_status": hb["status"], "orders_authorized": False}

    def items(self) -> list[dict[str, Any]]:
        state = self.status()
        if state["status"] not in {"HEALTHY", "DEGRADED"}:
            return []
        _, rows = self._read()
        return [row for row in rows if row.get("status") == "HEALTHY" and row.get("orders_authorized") is False]
