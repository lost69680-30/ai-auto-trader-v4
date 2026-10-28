"""Best-effort, server-only mirror of paper-ledger audit events to Supabase.

This adapter only writes audit rows. It has no order, risk, or exchange interface.
SQLite remains the authoritative ledger; Supabase writes are idempotent by source key.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Mapping, Sequence

logger = logging.getLogger(__name__)


def stable_source_event_key(source_id: str, event_id: int) -> str:
    """Return a deterministic idempotency key for one SQLite audit event."""
    source = source_id.strip()
    if not source or ":" in source or event_id < 1:
        raise ValueError("source_id must be non-empty and event_id must be positive")
    return f"{source}:{event_id}"


class SupabaseAuditMirror:
    """HTTPS REST writer. Credentials must only come from backend environment secrets."""

    def __init__(self, project_url: str, service_key: str, source_id: str,
                 *, timeout_seconds: float = 5.0) -> None:
        parsed = urllib.parse.urlparse(project_url.strip())
        if parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/"):
            raise ValueError("Supabase project URL must be an HTTPS origin")
        if not service_key.strip():
            raise ValueError("Supabase server key is required")
        if not source_id.strip() or ":" in source_id:
            raise ValueError("Supabase audit source ID is invalid")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._rest_url = f"{project_url.rstrip('/')}/rest/v1/audit_events?on_conflict=source_event_key"
        self._service_key = service_key
        self._source_id = source_id.strip()
        self._timeout = timeout_seconds

    @property
    def enabled(self) -> bool:
        return True

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "SupabaseAuditMirror | None":
        env = os.environ if environ is None else environ
        url = env.get("SUPABASE_URL", "").strip()
        key = env.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        source = env.get("SUPABASE_AUDIT_SOURCE_ID", "").strip()
        if not (url and key and source):
            return None
        try:
            return cls(url, key, source)
        except ValueError:
            logger.error("Supabase audit mirror disabled: invalid configuration; credentials omitted")
            return None

    @property
    def configured(self) -> bool:
        return True

    def mirror_events(self, events: Sequence[object]) -> int:
        """Send events individually; return count confirmed by successful HTTP responses."""
        delivered = 0
        for event in events:
            try:
                row = self._row(event)
                request = urllib.request.Request(
                    self._rest_url,
                    data=json.dumps([row], separators=(",", ":"), allow_nan=False).encode("utf-8"),
                    headers={
                        "apikey": self._service_key,
                        "Authorization": f"Bearer {self._service_key}",
                        "Content-Type": "application/json",
                        "Prefer": "resolution=ignore-duplicates,return=minimal",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    status = getattr(response, "status", 200)
                    response.read()
                if 200 <= status < 300:
                    delivered += 1
                else:
                    logger.warning("Supabase audit mirror received HTTP %s; event remains eligible for retry", status)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, TypeError, KeyError) as exc:
                # Deliberately do not log request headers, payloads, or exception text.
                logger.warning("Supabase audit mirror write failed (%s); event remains eligible for retry",
                               type(exc).__name__)
            except Exception as exc:
                logger.warning("Supabase audit mirror unexpected failure (%s); event remains eligible for retry",
                               type(exc).__name__)
        return delivered

    def _row(self, event: object) -> dict[str, str]:
        if isinstance(event, Mapping):
            get = event.__getitem__
        else:
            get = lambda key: getattr(event, key)
        event_id = int(get("event_id"))
        occurred_at = float(get("occurred_at"))
        if not (occurred_at >= 0 and occurred_at < float("inf")):
            raise ValueError("audit timestamp must be finite and non-negative")
        payload = {
            "event_id": event_id,
            "proposal_id": get("proposal_id"),
            "payload": get("payload"),
        }
        created_at = datetime.fromtimestamp(occurred_at, tz=timezone.utc).isoformat()
        return {
            "source_event_key": stable_source_event_key(self._source_id, event_id),
            "event_type": str(get("event_type"))[:64],
            "payload": json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False),
            "created_at": created_at,
        }


def run_mirror_loop(database_path: str, mirror: SupabaseAuditMirror,
                    *, interval_seconds: float = 30.0, stop_event=None) -> None:
    """Poll SQLite audit rows; restart-safe duplicate writes are ignored by Supabase."""
    from app.engines.paper_ledger import SQLitePaperLedger

    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    while stop_event is None or not stop_event.is_set():
        try:
            with SQLitePaperLedger(database_path) as ledger:
                events = ledger.audit_events
            if events:
                mirror.mirror_events(events)
        except Exception as exc:
            logger.warning("Supabase audit mirror poll failed (%s); SQLite remains authoritative",
                           type(exc).__name__)
        if stop_event is not None:
            if stop_event.wait(interval_seconds):
                break
        else:
            time.sleep(interval_seconds)
