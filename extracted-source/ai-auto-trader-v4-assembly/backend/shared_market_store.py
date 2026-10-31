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
        checked_dt = poll.checked_at
        if not isinstance(checked_dt, datetime) or checked_dt.tzinfo is None or checked_dt.utcoffset() is None:
            raise ValueError("poll checked_at must be timezone-aware")
        checked_dt = checked_dt.astimezone(timezone.utc)
        now_value = now or datetime.now(timezone.utc)
        if not isinstance(now_value, datetime) or now_value.tzinfo is None or now_value.utcoffset() is None:
            raise ValueError("store clock must be timezone-aware")
        now_dt = now_value.astimezone(timezone.utc)
        if (now_dt - checked_dt).total_seconds() < -1.0 or (now_dt - checked_dt).total_seconds() > self.max_age_seconds:
            raise ValueError("market poll timestamp is future-dated or stale")
        checked = _iso(checked_dt)
        stamp = _iso(now_dt)
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
            if frame.market_type not in {"FUTURES", "SPOT"}:
                raise ValueError("market frame has an unknown market type")
            observed_dt = frame.mark_observed_at
            if status == "HEALTHY":
                if frame.mark_price is None or _finite(frame.mark_price, "mark_price") <= 0:
                    raise ValueError("healthy market frame requires a positive mark price")
                if observed_dt is None:
                    raise ValueError("healthy market frame requires a mark observation timestamp")
                if _iso(observed_dt) is None:
                    raise ValueError("healthy market frame mark timestamp is missing")
                observed_dt = observed_dt.astimezone(timezone.utc)
                mark_age = (now_dt - observed_dt).total_seconds()
                if mark_age < -1.0 or mark_age > self.max_age_seconds:
                    raise ValueError("healthy market frame mark is future-dated or stale")
                if futures:
                    if frame.index_price is None or frame.funding_rate is None or frame.next_funding_at is None:
                        raise ValueError("healthy Futures frame requires complete index and funding context")
                    if _finite(frame.index_price, "index_price") <= 0:
                        raise ValueError("healthy Futures index price must be positive")
                    _finite(frame.funding_rate, "funding_rate")
                    next_funding_dt = frame.next_funding_at
                    if _iso(next_funding_dt) is None or next_funding_dt.astimezone(timezone.utc) <= now_dt:
                        raise ValueError("healthy Futures next funding timestamp must be in the future")
                elif any(value is not None for value in (frame.index_price, frame.funding_rate, frame.next_funding_at)):
                    raise ValueError("Spot market frame cannot contain Futures-only context")
                if not frame.closed_candles or any(not candle.is_closed for candle in frame.closed_candles):
                    raise ValueError("healthy market frame requires only fully closed candles")
            payload = {
                "symbol": frame.symbol, "status": status,
                "mark_price": None if frame.mark_price is None else _finite(frame.mark_price, "mark_price"),
                "last_price": None if futures or frame.mark_price is None else _finite(frame.mark_price, "last_price"),
                "mark_observed_at": _iso(frame.mark_observed_at),
                "last_observed_at": None if futures else _iso(frame.mark_observed_at),
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
                       status = %s, details = COALESCE(details, '{}'::jsonb) || %s::jsonb
                   WHERE worker_id = %s AND owner_id = %s RETURNING worker_id""",
                (stamp, stamp, self.lease_seconds, status,
                 json.dumps(details or {"orders_authorized": False}), self.worker_id, self.owner_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("market worker lease lost")

    def publish_intelligence(self, payload: dict[str, Any], *, now: datetime | None = None) -> None:
        """Persist the latest advisory scan inside the active worker heartbeat record."""
        if not isinstance(payload, dict) or payload.get("orders_authorized") is not False:
            raise ValueError("market intelligence payload must explicitly deny order authorization")
        assessed_at = payload.get("assessed_at")
        if not isinstance(assessed_at, str):
            raise ValueError("market intelligence assessed_at is required")
        try:
            assessed_dt = datetime.fromisoformat(assessed_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("market intelligence assessed_at is invalid") from exc
        if assessed_dt.tzinfo is None or assessed_dt.utcoffset() is None:
            raise ValueError("market intelligence assessed_at must be timezone-aware")
        current = now or datetime.now(timezone.utc)
        current_iso = _iso(current)
        if (current.astimezone(timezone.utc) - assessed_dt.astimezone(timezone.utc)).total_seconds() > max(
            1800.0, self.max_age_seconds * 20.0
        ):
            raise ValueError("market intelligence payload is already stale")
        symbols = payload.get("symbols")
        if not isinstance(symbols, list):
            raise ValueError("market intelligence symbols must be a list")
        if any(not isinstance(item, dict) or item.get("orders_authorized") is not False for item in symbols):
            raise ValueError("every market intelligence symbol must deny order authorization")
        body = json.dumps(payload, separators=(",", ":"), allow_nan=False)
        with self._connect() as conn:
            row = conn.execute(
                """UPDATE paper_ledger.market_worker_heartbeats
                   SET details = COALESCE(details, '{}'::jsonb)
                       || jsonb_build_object('market_intelligence', %s::jsonb)
                   WHERE worker_id = %s AND owner_id = %s
                   RETURNING worker_id""",
                (body, self.worker_id, self.owner_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("market worker lease lost before intelligence publication")

    def market_intelligence(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Return only a fresh advisory scan from the current worker lease."""
        hb, _rows = self._read()
        current = now or datetime.now(timezone.utc)
        current = current.astimezone(timezone.utc)
        if hb is None:
            return {"status": "STARTING", "reason": "market worker has not published a heartbeat",
                    "items": [], "orders_authorized": False}
        heartbeat_age = (current - hb["heartbeat_at"].astimezone(timezone.utc)).total_seconds()
        if (heartbeat_age < -1.0 or heartbeat_age > self.max_age_seconds
                or hb["lease_expires_at"] <= current or hb["status"] in {"STOPPED", "ERROR"}):
            return {"status": "STALE", "reason": "market worker heartbeat is stale or stopped",
                    "items": [], "orders_authorized": False}
        details = hb.get("details") or {}
        report = details.get("market_intelligence") if isinstance(details, dict) else None
        if not isinstance(report, dict) or report.get("orders_authorized") is not False:
            return {"status": "STARTING", "reason": "waiting for the first market intelligence scan",
                    "items": [], "orders_authorized": False}
        try:
            assessed_at = datetime.fromisoformat(str(report.get("assessed_at", "")).replace("Z", "+00:00"))
        except ValueError:
            assessed_at = None
        max_scan_age = max(1800.0, self.max_age_seconds * 20.0)
        if (assessed_at is None or assessed_at.tzinfo is None or assessed_at.utcoffset() is None
                or (current - assessed_at.astimezone(timezone.utc)).total_seconds() < -1.0
                or (current - assessed_at.astimezone(timezone.utc)).total_seconds() > max_scan_age):
            return {"status": "STALE", "reason": "market intelligence scan is stale or invalid",
                    "items": [], "orders_authorized": False}
        items = report.get("symbols")
        if not isinstance(items, list) or any(
            not isinstance(item, dict) or item.get("orders_authorized") is not False for item in items
        ):
            return {"status": "INVALID", "reason": "market intelligence payload failed safety validation",
                    "items": [], "orders_authorized": False}
        return {
            "status": "HEALTHY" if hb["status"] == "RUNNING" else "DEGRADED",
            "assessed_at": assessed_at.astimezone(timezone.utc).isoformat(),
            "market_type": report.get("market_type"),
            "scan_id": report.get("scan_id"),
            "items": items,
            "orders_authorized": False,
        }

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
                """SELECT worker_id, owner_id, heartbeat_at, lease_expires_at, status, details
                   FROM paper_ledger.market_worker_heartbeats
                   WHERE worker_id = %s""", (self.worker_id,)
            ).fetchone()
            rows = conn.execute(
                """SELECT payload FROM paper_ledger.market_feed_snapshots
                   WHERE worker_id = %s ORDER BY symbol""", (self.worker_id,)
            ).fetchall()
        return (None if hb is None else {
            "worker_id": hb[0], "owner_id": hb[1], "heartbeat_at": hb[2],
            "lease_expires_at": hb[3], "status": hb[4], "details": hb[5],
        }), [row[0] for row in rows]

    def status(self) -> dict[str, Any]:
        hb, rows = self._read()
        now = datetime.now(timezone.utc)
        if hb is None:
            return {"status": "STARTING", "reason": "no worker heartbeat", "checked_at": None,
                    "age_seconds": None, "worker_status": "MISSING", "orders_authorized": False}
        age = (now - hb["heartbeat_at"]).total_seconds()
        if age < 0 or age > self.max_age_seconds or hb["lease_expires_at"] <= now or hb["status"] in {"STOPPED", "ERROR"}:
            return {"status": "STALE", "reason": "market worker heartbeat is stale or stopped",
                    "checked_at": hb["heartbeat_at"].timestamp(), "age_seconds": age,
                    "worker_status": hb["status"], "orders_authorized": False}
        if hb["status"] == "STARTING":
            return {"status": "STARTING", "reason": "worker has not published its first validated market poll",
                    "checked_at": hb["heartbeat_at"].timestamp(), "age_seconds": age,
                    "worker_status": hb["status"], "orders_authorized": False}
        if not rows or any(
            (now - datetime.fromisoformat(row["checked_at"])).total_seconds() < -1.0
            or (now - datetime.fromisoformat(row["checked_at"])).total_seconds() > self.max_age_seconds
            for row in rows
        ):
            return {"status": "STALE", "reason": "market snapshots are missing or stale",
                    "checked_at": hb["heartbeat_at"].timestamp(), "age_seconds": age,
                    "worker_status": hb["status"], "orders_authorized": False}
        healthy = sum(1 for row in rows if row["status"] == "HEALTHY")
        feed_status = "HEALTHY" if healthy == len(rows) else ("DEGRADED" if healthy else "OFFLINE")
        return {"status": feed_status, "reason": None if feed_status == "HEALTHY" else "one or more symbols are unhealthy",
                "checked_at": hb["heartbeat_at"].timestamp(), "age_seconds": age,
                "worker_status": hb["status"], "orders_authorized": False}

    def analysis_status(self) -> dict[str, Any]:
        hb, _rows = self._read()
        if hb is None:
            return {
                "enabled": True, "status": "STARTING", "research_pipeline": "STARTING",
                "paper_simulation": "STARTING", "last_cycle_at": None,
                "last_cycle_error": None, "orders_authorized": False,
                "reason": "waiting for the independent paper-analysis worker",
            }
        age = (datetime.now(timezone.utc) - hb["heartbeat_at"]).total_seconds()
        if age < 0 or age > self.max_age_seconds or hb["lease_expires_at"] <= datetime.now(timezone.utc) or hb["status"] in {"STOPPED", "ERROR"}:
            return {
                "enabled": True, "status": "ERROR", "research_pipeline": "ERROR",
                "paper_simulation": "ERROR", "last_cycle_at": None,
                "last_cycle_error": "WorkerHeartbeatStale", "orders_authorized": False,
                "reason": "independent analysis worker heartbeat is stale or stopped",
            }
        details = hb.get("details") or {}
        cycle_status = str(details.get("analysis_status") or "STARTING")
        cycle_error = details.get("analysis_error")
        status = "ERROR" if (
            cycle_status.startswith("ANALYSIS_FAILED") or cycle_error or cycle_status in {
                "FUNDING_SYNC_ERROR", "MARKET_UNIVERSE_UNAVAILABLE",
                "MARKET_UNIVERSE_REJECTED", "AUTHORIZATION_FLAG_REJECTED",
                "ANALYSIS_RESULT_MISSING", "ANALYSIS_ELIGIBILITY_INVALID",
            }
        ) else (
            "DEGRADED" if cycle_status in {
                "SKIPPED_UNHEALTHY_MARKET_DATA", "FUNDING_SYNC_DEGRADED",
                "MARKET_UNIVERSE_STALE", "NO_HEALTHY_ALLOWLISTED_SYMBOLS",
                "NO_ELIGIBLE_SYMBOLS", "ANALYZED_WITH_EXCLUSIONS",
            } else ("RUNNING" if cycle_status != "STARTING" else "STARTING")
        )
        return {
            "enabled": True, "status": status, "research_pipeline": status,
            "paper_simulation": status, "last_cycle_at": details.get("last_cycle_at"),
            "last_cycle_error": cycle_error, "orders_authorized": False,
            "reason": cycle_status[:160],
        }

    def items(self) -> list[dict[str, Any]]:
        state = self.status()
        if state["status"] not in {"HEALTHY", "DEGRADED"}:
            return []
        _, rows = self._read()
        return [row for row in rows if row.get("status") == "HEALTHY" and row.get("orders_authorized") is False]
