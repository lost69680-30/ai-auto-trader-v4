"""Append-only PostgreSQL memory for provenance-backed strategy experiments."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from typing import Any, Mapping

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - deployment dependency
    psycopg = None
    dict_row = None

from app.engines.postgres_ssl import postgres_connection_ssl_options


_ALLOWED_STATUSES = {
    "EVALUATED", "REJECTED", "INSUFFICIENT_DATA",
    "DRIFT_FLAGGED", "DECAY_FLAGGED", "NOT_REPRODUCIBLE",
}


def _required_text(value: object, name: str, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a trimmed non-empty string")
    return value


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _paper_json(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")

    def inspect(item: Any, path: str) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key == "orders_authorized" and child is not False:
                    raise ValueError(f"{path}.orders_authorized must be false")
                inspect(child, f"{path}.{key}")
        elif isinstance(item, (list, tuple)):
            for index, child in enumerate(item):
                inspect(child, f"{path}[{index}]")
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError(f"{path} contains a non-finite number")

    result = dict(value)
    inspect(result, name)
    try:
        encoded = json.dumps(result, allow_nan=False, separators=(",", ":"))
        return json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain finite JSON-compatible values") from exc


@dataclass(frozen=True, slots=True)
class StrategyResearchExperiment:
    experiment_id: str
    candidate_id: str
    candidate_fingerprint: str
    source_provenance_id: str
    experiment_version: int
    dataset_start: datetime
    dataset_end: datetime
    configuration_version: str
    assumptions: Mapping[str, Any]
    outcomes: Mapping[str, Any]
    status: str
    failure_reasons: tuple[str, ...] = ()
    orders_authorized: bool = False

    def __post_init__(self) -> None:
        _required_text(self.experiment_id, "experiment_id", 160)
        _required_text(self.candidate_id, "candidate_id", 160)
        _required_text(self.candidate_fingerprint, "candidate_fingerprint", 128)
        _required_text(self.source_provenance_id, "source_provenance_id", 128)
        _required_text(self.configuration_version, "configuration_version", 120)
        if (
            isinstance(self.experiment_version, bool)
            or not isinstance(self.experiment_version, int)
            or self.experiment_version < 1
        ):
            raise ValueError("experiment_version must be a positive integer")
        start = _utc(self.dataset_start, "dataset_start")
        end = _utc(self.dataset_end, "dataset_end")
        if start >= end:
            raise ValueError("dataset window must have start before end")
        if self.status not in _ALLOWED_STATUSES:
            raise ValueError("experiment status is invalid")
        if not isinstance(self.failure_reasons, tuple) or any(
            not isinstance(reason, str) or not reason.strip() for reason in self.failure_reasons
        ):
            raise ValueError("failure_reasons must be a tuple of non-empty strings")
        object.__setattr__(self, "assumptions", _paper_json(self.assumptions, "assumptions"))
        object.__setattr__(self, "outcomes", _paper_json(self.outcomes, "outcomes"))
        if self.orders_authorized is not False:
            raise ValueError("orders_authorized must be false")


@dataclass(frozen=True, slots=True)
class StrategyResearchAppendResult:
    experiment: StrategyResearchExperiment
    duplicate: bool


class PostgresStrategyResearchMemory:
    """Append-only durable experiment history; no update/delete interface exists."""

    def __init__(self, dsn: str) -> None:
        if psycopg is None:
            raise RuntimeError("Strategy research memory requires psycopg")
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("PAPER_DATABASE_URL is required for strategy research memory")
        self._dsn = dsn.strip()

    def _connect(self):
        return psycopg.connect(
            self._dsn,
            connect_timeout=10,
            row_factory=dict_row,
            **postgres_connection_ssl_options(self._dsn),
        )

    @staticmethod
    def _from_row(row: dict[str, Any]) -> StrategyResearchExperiment:
        return StrategyResearchExperiment(
            experiment_id=row["experiment_id"],
            candidate_id=row["candidate_id"],
            candidate_fingerprint=row["candidate_fingerprint"],
            source_provenance_id=row["source_provenance_id"],
            experiment_version=row["experiment_version"],
            dataset_start=row["dataset_start"],
            dataset_end=row["dataset_end"],
            configuration_version=row["configuration_version"],
            assumptions=row["assumptions"],
            outcomes=row["outcomes"],
            status=row["status"],
            failure_reasons=tuple(row["failure_reasons"]),
            orders_authorized=row["orders_authorized"],
        )

    def append(
        self,
        experiment: StrategyResearchExperiment,
        *,
        idempotency_key: str,
    ) -> StrategyResearchAppendResult:
        if not isinstance(experiment, StrategyResearchExperiment):
            raise ValueError("experiment must be a StrategyResearchExperiment")
        key = _required_text(idempotency_key, "idempotency_key", 200)
        assumptions = _paper_json(experiment.assumptions, "assumptions")
        outcomes = _paper_json(experiment.outcomes, "outcomes")
        with self._connect() as conn:
            inserted = conn.execute(
                """INSERT INTO paper_ledger.strategy_research_experiments
                   (experiment_id, idempotency_key, candidate_id, candidate_fingerprint,
                    source_provenance_id, experiment_version, dataset_start, dataset_end,
                    configuration_version, assumptions, outcomes, status, failure_reasons,
                    orders_authorized)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                           %s, %s::jsonb, false)
                   ON CONFLICT DO NOTHING
                   RETURNING *""",
                (
                    experiment.experiment_id, key, experiment.candidate_id,
                    experiment.candidate_fingerprint, experiment.source_provenance_id,
                    experiment.experiment_version, _utc(experiment.dataset_start, "dataset_start"),
                    _utc(experiment.dataset_end, "dataset_end"), experiment.configuration_version,
                    json.dumps(assumptions, allow_nan=False, separators=(",", ":")),
                    json.dumps(outcomes, allow_nan=False, separators=(",", ":")),
                    experiment.status,
                    json.dumps(list(experiment.failure_reasons), allow_nan=False),
                ),
            ).fetchone()
            if inserted is not None:
                return StrategyResearchAppendResult(self._from_row(inserted), False)

            existing = conn.execute(
                """SELECT * FROM paper_ledger.strategy_research_experiments
                   WHERE idempotency_key = %s""",
                (key,),
            ).fetchone()
            if existing is not None:
                stored = self._from_row(existing)
                if stored != experiment:
                    raise ValueError("idempotency key conflicts with a different experiment")
                return StrategyResearchAppendResult(stored, True)

            version = conn.execute(
                """SELECT experiment_id FROM paper_ledger.strategy_research_experiments
                   WHERE candidate_fingerprint = %s AND experiment_version = %s""",
                (experiment.candidate_fingerprint, experiment.experiment_version),
            ).fetchone()
            if version is not None:
                raise ValueError("candidate experiment version already exists")
            raise RuntimeError("strategy research experiment insert failed without a matching conflict")

    def list_by_candidate(
        self, candidate_fingerprint: str,
    ) -> tuple[StrategyResearchExperiment, ...]:
        fingerprint = _required_text(candidate_fingerprint, "candidate_fingerprint", 128)
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM paper_ledger.strategy_research_experiments
                   WHERE candidate_fingerprint = %s
                   ORDER BY experiment_version ASC""",
                (fingerprint,),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)
