"""Strictly allowlisted dispatcher for read-only scheduled analysis jobs."""
from __future__ import annotations

import loggingimport uuid
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any], str], None]


class AnalysisJobDispatcher:
    """Claim and execute only explicitly registered read-only analysis handlers.

    Queue delivery is at-least-once. Every handler receives the durable
    idempotency key and must use it for any externally visible side effect.
    This dispatcher deliberately has no order, fill, position, or risk-setting
    handlers and rejects any claimed job that asserts order authority.
    """

    def __init__(
        self, scheduler: Any, *, owner: str | None = None,
        handlers: dict[str, Handler] | None = None,
    ) -> None:
        self.scheduler = scheduler
        self.owner = owner or f"analysis-{uuid.uuid4()}"
        if not isinstance(self.owner, str) or not self.owner.strip() or len(self.owner) > 120:
            raise ValueError("dispatcher owner must contain 1 to 120 characters")
        self.handlers = dict(handlers or {})
        if any(not isinstance(name, str) or not callable(handler)
               for name, handler in self.handlers.items()):
            raise ValueError("analysis handlers must map names to callable functions")
        forbidden = {
            "place_order", "execute_trade", "create_fill", "change_risk_limits",
            "paper_order_execution", "order_execution", "position_mutation",
        }
        if forbidden.intersection(self.handlers):
            raise ValueError("execution and risk mutation handlers are forbidden")

    def run_once(self) -> bool:
        job = self.scheduler.claim_next(owner=self.owner)
        if job is None:
            return False
        key = job.get("idempotency_key")
        name = job.get("job_name")
        try:
            if job.get("orders_authorized") is not False:
                raise ValueError("job authorization flag must be explicitly false")
            if not isinstance(key, str) or not key:
                raise ValueError("job idempotency key is missing")
            if not isinstance(name, str) or name not in self.handlers:
                raise ValueError(f"job type {name!r} is not allowlisted")
            payload = job.get("payload")
            if not isinstance(payload, dict):
                raise ValueError("analysis job payload must be an object")
            self.handlers[name](payload, key)
        except Exception as exc:
            safe_error = f"{type(exc).__name__}: {str(exc)}"[:240]
            logger.exception("scheduled analysis job rejected or failed job=%s", name)
            if isinstance(key, str) and key:
                self.scheduler.fail(key, owner=self.owner, error=safe_error)
            return False

        if not self.scheduler.complete(key, owner=self.owner):
            logger.warning("scheduled analysis job lease lost before completion job=%s", name)
            return False
        logger.info("scheduled read-only analysis job completed job=%s orders_authorized=False", name)
        return True


def market_feed_health_audit(store: Any) -> Handler:
    """Build a read-only handler that logs feed health without mutating trading state."""
    def handle(payload: dict[str, Any], idempotency_key: str) -> None:
        if payload.get("scope") != "market_feed":
            raise ValueError("market feed health audit requires scope=market_feed")
        status = store.status()
        logger.info(
            "scheduled market feed health audit key=%s status=%s worker_status=%s orders_authorized=False",
            idempotency_key, status.get("status"), status.get("worker_status"),
        )
    return handle
