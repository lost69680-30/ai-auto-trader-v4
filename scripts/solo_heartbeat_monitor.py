from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping


CONFIRMED_STOP_STATES = {"STOPPED", "ERROR", "STALE", "MISSING"}
DEFAULT_SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "TRXUSDT", "LINKUSDT", "AVAXUSDT",
)
SNAPSHOT_QUERY = """
SELECT
    h.status,
    h.heartbeat_at,
    h.lease_expires_at,
    h.orders_authorized,
    h.details,
    f.total_rows,
    f.healthy_rows,
    f.stale_rows,
    f.unsafe_rows,
    f.symbols,
    f.latest_checked_at
FROM paper_ledger.market_worker_heartbeats AS h
LEFT JOIN LATERAL (
    SELECT
        count(*)::integer AS total_rows,
        count(*) FILTER (WHERE feed_status = 'HEALTHY')::integer AS healthy_rows,
        count(*) FILTER (
            WHERE checked_at > now() + interval '1 second'
               OR checked_at < now() - (%s * interval '1 second')
        )::integer AS stale_rows,
        count(*) FILTER (WHERE orders_authorized IS DISTINCT FROM false)::integer AS unsafe_rows,
        COALESCE(array_agg(symbol ORDER BY symbol), ARRAY[]::text[]) AS symbols,
        max(checked_at) AS latest_checked_at
    FROM paper_ledger.market_feed_snapshots
    WHERE worker_id = h.worker_id
) AS f ON true
WHERE h.worker_id = %s
"""


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _age_seconds(value: Any, now: datetime) -> float | None:
    parsed = _timestamp(value)
    if parsed is None:
        return None
    age = (now - parsed).total_seconds()
    return age if math.isfinite(age) else None


def classify_worker_snapshot(
    snapshot: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
    max_heartbeat_age_seconds: float = 90.0,
    max_scheduler_stale_seconds: float = 900.0,
    max_snapshot_age_seconds: float = 90.0,
    expected_symbols: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Classify worker health without waking, restarting, or mutating the worker."""
    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    checked_at = checked_at.astimezone(timezone.utc)

    for value, name in (
        (max_heartbeat_age_seconds, "max_heartbeat_age_seconds"),
        (max_scheduler_stale_seconds, "max_scheduler_stale_seconds"),
        (max_snapshot_age_seconds, "max_snapshot_age_seconds"),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{name} must be a finite positive number")

    if not isinstance(snapshot, Mapping):
        return {
            "state": "UNKNOWN",
            "worker_process_healthy": False,
            "scheduler_healthy": False,
            "market_data_ready": False,
            "orders_authorized": False,
            "reasons": ["heartbeat_missing"],
        }

    authorization = snapshot.get("orders_authorized")
    if authorization is True:
        return {
            "state": "SAFETY_VIOLATION",
            "worker_process_healthy": False,
            "scheduler_healthy": False,
            "market_data_ready": False,
            "orders_authorized": True,
            "reasons": ["orders_authorized_true"],
        }
    if authorization is not False:
        return {
            "state": "UNKNOWN",
            "worker_process_healthy": False,
            "scheduler_healthy": False,
            "market_data_ready": False,
            "orders_authorized": None,
            "reasons": ["orders_authorization_unknown"],
        }

    status = snapshot.get("status")
    reasons: list[str] = []
    if status in CONFIRMED_STOP_STATES:
        reasons.append(f"worker_status_{str(status).lower()}")
    elif status != "RUNNING":
        reasons.append("worker_not_running")

    heartbeat_age = _age_seconds(snapshot.get("heartbeat_at"), checked_at)
    if heartbeat_age is None:
        reasons.append("heartbeat_timestamp_invalid")
    elif heartbeat_age < -1.0 or heartbeat_age > max_heartbeat_age_seconds:
        reasons.append("heartbeat_stale")

    lease_expiry = _timestamp(snapshot.get("lease_expires_at"))
    if lease_expiry is None:
        reasons.append("lease_timestamp_invalid")
    elif lease_expiry <= checked_at:
        reasons.append("lease_expired")

    worker_healthy = (
        status in {"RUNNING", "DEGRADED"}
        and heartbeat_age is not None
        and -1.0 <= heartbeat_age <= max_heartbeat_age_seconds
        and lease_expiry is not None
        and lease_expiry > checked_at
    )
    if not worker_healthy:
        return {
            "state": "STOPPED" if status in CONFIRMED_STOP_STATES or "heartbeat_stale" in reasons or "lease_expired" in reasons else "UNKNOWN",
            "worker_process_healthy": False,
            "scheduler_healthy": False,
            "market_data_ready": False,
            "orders_authorized": False,
            "heartbeat_age_seconds": heartbeat_age,
            "reasons": reasons,
        }

    details = snapshot.get("details")
    scheduler = details.get("scheduled_jobs") if isinstance(details, Mapping) else None
    if not isinstance(scheduler, Mapping):
        scheduler = {}
    scheduler_status = scheduler.get("status")
    scheduler_age = _age_seconds(scheduler.get("last_poll_at"), checked_at)
    scheduler_healthy = (
        scheduler_status == "RUNNING"
        and scheduler_age is not None
        and -1.0 <= scheduler_age <= max_scheduler_stale_seconds
    )
    if not scheduler_healthy:
        reasons.append("scheduler_stale")

    feed = snapshot.get("feed")
    if not isinstance(feed, Mapping):
        feed = {}
    try:
        total_rows = int(feed.get("total_rows", 0))
        healthy_rows = int(feed.get("healthy_rows", 0))
        stale_rows = int(feed.get("stale_rows", 0))
        unsafe_rows = int(feed.get("unsafe_rows", 0))
    except (TypeError, ValueError, OverflowError):
        total_rows = healthy_rows = stale_rows = 0
        unsafe_rows = 0
        reasons.append("feed_counts_invalid")

    if unsafe_rows > 0:
        return {
            "state": "SAFETY_VIOLATION",
            "worker_process_healthy": True,
            "scheduler_healthy": scheduler_healthy,
            "market_data_ready": False,
            "orders_authorized": False,
            "heartbeat_age_seconds": heartbeat_age,
            "reasons": reasons + ["feed_orders_authorization_unsafe"],
        }

    symbols = feed.get("symbols")
    observed_symbols = set(symbols) if isinstance(symbols, (list, tuple, set)) else set()
    expected = set(expected_symbols) if expected_symbols is not None else None
    symbol_coverage_ok = expected is None or observed_symbols == expected
    snapshot_age = _age_seconds(feed.get("latest_checked_at"), checked_at)
    snapshot_fresh = (
        snapshot_age is not None
        and -1.0 <= snapshot_age <= max_snapshot_age_seconds
    )
    market_data_ready = (
        total_rows > 0
        and healthy_rows == total_rows
        and stale_rows == 0
        and snapshot_fresh
        and symbol_coverage_ok
    )
    if not market_data_ready:
        if not symbol_coverage_ok:
            reasons.append("symbol_coverage_mismatch")
        if not snapshot_fresh or stale_rows > 0:
            reasons.append("market_snapshot_stale")
        if total_rows <= 0 or healthy_rows != total_rows:
            reasons.append("market_feed_degraded")

    state = "HEALTHY" if status == "RUNNING" and scheduler_healthy and market_data_ready else "DEGRADED"
    return {
        "state": state,
        "worker_process_healthy": True,
        "scheduler_healthy": scheduler_healthy,
        "market_data_ready": market_data_ready,
        "orders_authorized": False,
        "heartbeat_age_seconds": round(heartbeat_age, 2) if heartbeat_age is not None else None,
        "scheduler_age_seconds": round(scheduler_age, 2) if scheduler_age is not None else None,
        "snapshot_age_seconds": round(snapshot_age, 2) if snapshot_age is not None else None,
        "healthy_symbols": sorted(observed_symbols),
        "reasons": reasons,
    }


def load_worker_snapshot(database_url: str, worker_id: str, max_snapshot_age_seconds: float) -> dict[str, Any] | None:
    """Read the worker heartbeat and market snapshots using a read-only connection."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(
        database_url,
        connect_timeout=10,
        options="-c default_transaction_read_only=on",
        sslmode="require",
    ) as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(SNAPSHOT_QUERY, (max_snapshot_age_seconds, worker_id))
            row = cursor.fetchone()
    if row is None:
        return None
    return {
        "status": row["status"],
        "heartbeat_at": row["heartbeat_at"],
        "lease_expires_at": row["lease_expires_at"],
        "orders_authorized": row["orders_authorized"],
        "details": row["details"],
        "feed": {
            "total_rows": row["total_rows"] or 0,
            "healthy_rows": row["healthy_rows"] or 0,
            "stale_rows": row["stale_rows"] or 0,
            "unsafe_rows": row["unsafe_rows"] or 0,
            "symbols": row["symbols"] or [],
            "latest_checked_at": row["latest_checked_at"],
        },
    }


def _summary_markdown(result: Mapping[str, Any], worker_id: str) -> str:
    reasons = ", ".join(result.get("reasons") or []) or "none"
    return (
        "## Solo independent worker heartbeat monitor\n"
        f"- Worker: `{worker_id}`\n"
        f"- State: **{result.get('state', 'UNKNOWN')}**\n"
        f"- Worker process healthy: `{result.get('worker_process_healthy')}`\n"
        f"- Scheduler healthy: `{result.get('scheduler_healthy')}`\n"
        f"- Market data ready: `{result.get('market_data_ready')}`\n"
        f"- Orders authorized: `{result.get('orders_authorized')}`\n"
        f"- Heartbeat age: `{result.get('heartbeat_age_seconds')}` seconds\n"
        f"- Reasons: {reasons}\n\n"
        "This monitor uses read-only database access. It does not ping, wake, restart, or mutate the worker.\n"
    )


def main() -> int:
    database_url = os.environ.get("SOLO_BACKUP_DATABASE_URL", "").strip()
    if not database_url:
        print("ERROR: SOLO_BACKUP_DATABASE_URL is not configured.")
        return 2
    worker_id = os.environ.get("PAPER_WORKER_ID", "solo-market-worker").strip()
    if not worker_id:
        print("ERROR: PAPER_WORKER_ID is empty.")
        return 2

    try:
        max_heartbeat_age = float(os.environ.get("PAPER_MAX_MARKET_AGE_SECONDS", "90"))
        max_scheduler_age = float(os.environ.get("PAPER_MAX_SCHEDULER_STALE_SECONDS", "900"))
        max_snapshot_age = float(os.environ.get("PAPER_MAX_MARKET_AGE_SECONDS", "90"))
        raw_symbols = os.environ.get("PAPER_SYMBOLS", "").strip()
        if not raw_symbols:
            raw_symbols = ",".join(DEFAULT_SYMBOLS)
        expected_symbols = tuple(
            symbol.strip().upper()
            for symbol in raw_symbols.split(",")
            if symbol.strip()
        )
        if not expected_symbols:
            raise ValueError("PAPER_SYMBOLS must contain at least one symbol")
        snapshot = load_worker_snapshot(database_url, worker_id, max_snapshot_age)
        result = classify_worker_snapshot(
            snapshot,
            max_heartbeat_age_seconds=max_heartbeat_age,
            max_scheduler_stale_seconds=max_scheduler_age,
            max_snapshot_age_seconds=max_snapshot_age,
            expected_symbols=expected_symbols,
        )
    except Exception as exc:
        result = {
            "state": "UNKNOWN",
            "worker_process_healthy": False,
            "scheduler_healthy": False,
            "market_data_ready": False,
            "orders_authorized": None,
            "reasons": [f"read_only_monitor_error_{type(exc).__name__}"],
        }

    print(json.dumps(result, sort_keys=True, default=str))
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        Path(summary_path).write_text(_summary_markdown(result, worker_id), encoding="utf-8")

    if result["state"] == "HEALTHY":
        return 0
    if result["state"] == "SAFETY_VIOLATION":
        print("::error::Solo worker or market snapshot authorization invariant failed.")
        return 3
    if result["state"] == "UNKNOWN":
        print("::error::Solo worker health is unknown; no restart was attempted.")
        return 2
    print("::warning::Solo worker is stopped or degraded; no wake or restart was attempted.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
