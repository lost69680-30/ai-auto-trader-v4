"""PostgreSQL-backed recurring schedule and idempotent job queue."""
from __future__ import annotations

import json
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

from app.engines.postgres_ssl import postgres_connection_ssl_options


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scheduler timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


class DurableJobScheduler:
    """Persist recurring schedules and lease jobs with exactly-once enqueue semantics.

    Execution is at-least-once: a crashed worker may retry an expired lease. Handlers
    must use the job's idempotency_key for any side effect. This queue itself never
    grants exchange order authority.
    """

    def __init__(self, dsn: str, *, lease_seconds: float = 60.0) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("PAPER_DATABASE_URL is required for durable scheduling")
        if not math.isfinite(lease_seconds) or lease_seconds < 5:
            raise ValueError("lease_seconds must be at least 5 seconds")
        self._dsn = dsn.strip()
        self.lease_seconds = float(lease_seconds)

    def _connect(self):
        return psycopg.connect(
            self._dsn, connect_timeout=10, **postgres_connection_ssl_options(self._dsn)
        )

    @staticmethod
    def _validate_name(name: str) -> str:
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{1,79}", name):
            raise ValueError("job_name must be a lowercase identifier")
        return name

    def register_interval(
        self, job_name: str, *, interval_seconds: int,
        first_run_at: datetime, payload: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> None:
        name = self._validate_name(job_name)
        if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int) or interval_seconds < 1:
            raise ValueError("interval_seconds must be a positive integer")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        if payload is not None and not isinstance(payload, dict):
            raise ValueError("schedule payload must be a JSON object")
        body = json.dumps(payload or {}, separators=(",", ":"), allow_nan=False)
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO paper_ledger.scheduled_job_definitions
                   (job_name, interval_seconds, next_run_at, enabled, payload)
                   VALUES (%s, %s, %s, %s, %s::jsonb)
                   ON CONFLICT (job_name) DO UPDATE SET
                     interval_seconds = EXCLUDED.interval_seconds,
                     enabled = EXCLUDED.enabled, payload = EXCLUDED.payload,
                     updated_at = now()""",
                (name, interval_seconds, _utc(first_run_at), enabled, body),
            )

    def enqueue_due(self, *, now: datetime | None = None, max_catchup_per_schedule: int = 100) -> int:
        stamp = _utc(now or datetime.now(timezone.utc))
        if isinstance(max_catchup_per_schedule, bool) or not isinstance(max_catchup_per_schedule, int) or not 1 <= max_catchup_per_schedule <= 1000:
            raise ValueError("max_catchup_per_schedule must be between 1 and 1000")
        inserted = 0
        with self._connect() as conn:
            schedules = conn.execute(
                """SELECT job_name, interval_seconds, next_run_at, payload
                   FROM paper_ledger.scheduled_job_definitions
                   WHERE enabled = true AND next_run_at <= %s
                   ORDER BY next_run_at, job_name FOR UPDATE""", (stamp,)
            ).fetchall()
            for name, interval, next_run, payload in schedules:
                due = next_run
                processed = 0
                while due <= stamp and processed < max_catchup_per_schedule:
                    key = f"{name}:{due.astimezone(timezone.utc).isoformat()}"
                    result = conn.execute(
                        """INSERT INTO paper_ledger.scheduled_jobs
                           (idempotency_key, job_name, scheduled_for, status, payload)
                           VALUES (%s, %s, %s, 'PENDING', %s::jsonb)
                           ON CONFLICT (idempotency_key) DO NOTHING
                           RETURNING idempotency_key""",
                        (key, name, due, json.dumps(payload, separators=(",", ":"), allow_nan=False)),
                    ).fetchone()
                    inserted += int(result is not None)
                    due += timedelta(seconds=interval)
                    processed += 1
                conn.execute(
                    """UPDATE paper_ledger.scheduled_job_definitions
                       SET next_run_at = %s, updated_at = now() WHERE job_name = %s""",
                    (due, name),
                )
        return inserted

    def claim_next(
        self, *, owner: str | None = None, now: datetime | None = None,
        max_attempts: int = 5,
    ) -> dict[str, Any] | None:
        stamp = _utc(now or datetime.now(timezone.utc))
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        lease_owner = owner or str(uuid.uuid4())
        if not isinstance(lease_owner, str) or not lease_owner.strip() or len(lease_owner) > 120:
            raise ValueError("owner must contain 1 to 120 characters")
        with self._connect() as conn:
            conn.execute(
                """UPDATE paper_ledger.scheduled_jobs
                   SET status = 'FAILED', lease_owner = NULL, lease_expires_at = NULL,
                       completed_at = %s, last_error = COALESCE(last_error, 'lease expired after max attempts'),
                       updated_at = now()
                   WHERE status = 'RUNNING' AND lease_expires_at <= %s AND attempts >= %s""",
                (stamp, stamp, max_attempts),
            )
            row = conn.execute(
                """WITH candidate AS (
                     SELECT idempotency_key
                     FROM paper_ledger.scheduled_jobs
                     WHERE (status = 'PENDING'
                            OR (status = 'RUNNING' AND lease_expires_at <= %s))
                       AND attempts < %s
                     ORDER BY scheduled_for, idempotency_key
                     FOR UPDATE SKIP LOCKED LIMIT 1
                   )
                   UPDATE paper_ledger.scheduled_jobs AS job
                   SET status = 'RUNNING', attempts = job.attempts + 1,
                       lease_owner = %s,
                       lease_expires_at = %s + (%s * interval '1 second'),
                       started_at = COALESCE(job.started_at, %s),
                       updated_at = now()
                   FROM candidate
                   WHERE job.idempotency_key = candidate.idempotency_key
                   RETURNING job.idempotency_key, job.job_name, job.scheduled_for,
                             job.status, job.attempts, job.lease_owner,
                             job.lease_expires_at, job.payload""",
                (stamp, max_attempts, lease_owner, stamp, self.lease_seconds, stamp),
            ).fetchone()
        if row is None:
            return None
        return {
            "idempotency_key": row[0], "job_name": row[1], "scheduled_for": row[2],
            "status": row[3], "attempts": row[4], "lease_owner": row[5],
            "lease_expires_at": row[6], "payload": row[7], "orders_authorized": False,
        }

    def renew(self, idempotency_key: str, *, owner: str, now: datetime | None = None) -> bool:
        stamp = _utc(now or datetime.now(timezone.utc))
        with self._connect() as conn:
            row = conn.execute(
                """UPDATE paper_ledger.scheduled_jobs
                   SET lease_expires_at = %s + (%s * interval '1 second'), updated_at = now()
                   WHERE idempotency_key = %s AND status = 'RUNNING'
                     AND lease_owner = %s AND lease_expires_at > %s
                   RETURNING idempotency_key""",
                (stamp, self.lease_seconds, idempotency_key, owner, stamp),
            ).fetchone()
        return row is not None

    def complete(self, idempotency_key: str, *, owner: str, now: datetime | None = None) -> bool:
        stamp = _utc(now or datetime.now(timezone.utc))
        with self._connect() as conn:
            row = conn.execute(
                """UPDATE paper_ledger.scheduled_jobs
                   SET status = 'SUCCEEDED', completed_at = %s,
                       lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
                   WHERE idempotency_key = %s AND status = 'RUNNING'
                     AND lease_owner = %s AND lease_expires_at > %s
                   RETURNING idempotency_key""",
                (stamp, idempotency_key, owner, stamp),
            ).fetchone()
        return row is not None

    def fail(
        self, idempotency_key: str, *, owner: str, error: str,
        now: datetime | None = None, max_attempts: int = 5,
    ) -> str:
        stamp = _utc(now or datetime.now(timezone.utc))
        if not isinstance(error, str) or not error.strip():
            raise ValueError("error description is required")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        safe_error = " ".join(error.split())[:240]
        with self._connect() as conn:
            row = conn.execute(
                """UPDATE paper_ledger.scheduled_jobs
                   SET status = CASE WHEN attempts >= %s THEN 'FAILED' ELSE 'PENDING' END,
                       last_error = %s, completed_at = CASE WHEN attempts >= %s THEN %s ELSE NULL END,
                       lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
                   WHERE idempotency_key = %s AND status = 'RUNNING'
                     AND lease_owner = %s AND lease_expires_at > %s
                   RETURNING status""",
                (max_attempts, safe_error, max_attempts, stamp, idempotency_key, owner, stamp),
            ).fetchone()
        if row is None:
            raise RuntimeError("job lease is no longer owned by this worker")
        return row[0]
