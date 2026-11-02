"""Isolated research-only learning contracts for PPO/RL, GAT and specialist agents.

This module prepares validated Part 6 evidence for external learning workers. It
does not implement training, change risk settings, promote strategies or execute.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from typing import Any

from backend.strategy_research_memory import StrategyResearchExperiment


_AGENT_KINDS = {"PPO", "RL", "GAT", "SPECIALIST"}
_FORBIDDEN_WEIGHT_TERMS = {
    "risk", "leverage", "loss", "allocation", "order", "position",
    "orders_authorized", "stop", "take_profit", "takeprofit",
    "capital", "exposure", "equity", "drawdown", "margin", "notional",
    "quantity", "collateral", "liquidation", "portfolio",
}


def _text(value: object, name: str, maximum: int = 160) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a trimmed non-empty string")
    return value


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


@dataclass(frozen=True, slots=True)
class ResearchDatasetSnapshot:
    dataset_fingerprint: str
    source_experiment_ids: tuple[str, ...]
    validation_rows: tuple[Mapping[str, Any], ...]
    orders_authorized: bool = False

    def __post_init__(self) -> None:
        _text(self.dataset_fingerprint, "dataset_fingerprint", 128)
        if not isinstance(self.source_experiment_ids, tuple) or not self.source_experiment_ids:
            raise ValueError("source_experiment_ids must be a non-empty tuple")
        if len(self.source_experiment_ids) != len(set(self.source_experiment_ids)):
            raise ValueError("source experiment IDs must be unique")
        if not isinstance(self.validation_rows, tuple) or not self.validation_rows:
            raise ValueError("validation_rows must be a non-empty tuple")
        allowed_ids = set(self.source_experiment_ids)
        for row in self.validation_rows:
            if not isinstance(row, Mapping) or row.get("experiment_id") not in allowed_ids:
                raise ValueError("validation row provenance is invalid")
            if "untouched_holdout" in row or "holdout" in row:
                raise ValueError("final holdout data cannot enter a learning dataset")
            if row.get("orders_authorized", False) is not False:
                raise ValueError("orders_authorized must be false")
        if self.orders_authorized is not False:
            raise ValueError("orders_authorized must be false")


@dataclass(frozen=True, slots=True)
class LearningProposalValidation:
    status: str
    allowed_experiment_ids: tuple[str, ...]
    orders_authorized: bool = False


@dataclass(frozen=True, slots=True)
class ResearchLearningProposal:
    proposal_id: str
    agent_id: str
    agent_kind: str
    dataset_fingerprint: str
    source_experiment_ids: tuple[str, ...]
    rationale: str
    weights: Mapping[str, int | float]
    orders_authorized: bool = False

    def __post_init__(self) -> None:
        _text(self.proposal_id, "proposal_id")
        _text(self.agent_id, "agent_id")
        if self.agent_kind not in _AGENT_KINDS:
            raise ValueError("agent_kind must be PPO, RL, GAT or SPECIALIST")
        _text(self.dataset_fingerprint, "dataset_fingerprint", 128)
        _text(self.rationale, "rationale", 1000)
        if not isinstance(self.source_experiment_ids, tuple) or not self.source_experiment_ids:
            raise ValueError("source_experiment_ids must be a non-empty tuple")
        for experiment_id in self.source_experiment_ids:
            _text(experiment_id, "source experiment id")
        if not isinstance(self.weights, Mapping):
            raise ValueError("weights must be a mapping")
        for key, value in self.weights.items():
            name = _text(key, "weight name", 80).lower()
            if any(term in name for term in _FORBIDDEN_WEIGHT_TERMS):
                raise ValueError("learning proposals cannot modify risk or execution settings")
            _finite(value, f"weight {key}")
        if self.orders_authorized is not False:
            raise ValueError("orders_authorized must be false")


@dataclass(frozen=True, slots=True)
class IndependentLearningEvaluation:
    evaluation_dataset_fingerprint: str
    sample_size: int
    candidate_net_return_pct: float
    baseline_net_return_pct: float
    candidate_max_drawdown_pct: float
    baseline_max_drawdown_pct: float
    orders_authorized: bool = False

    def __post_init__(self) -> None:
        _text(self.evaluation_dataset_fingerprint, "evaluation_dataset_fingerprint", 128)
        if isinstance(self.sample_size, bool) or not isinstance(self.sample_size, int) or self.sample_size < 0:
            raise ValueError("sample_size must be a non-negative integer")
        _finite(self.candidate_net_return_pct, "candidate_net_return_pct")
        _finite(self.baseline_net_return_pct, "baseline_net_return_pct")
        _finite(self.candidate_max_drawdown_pct, "candidate_max_drawdown_pct")
        _finite(self.baseline_max_drawdown_pct, "baseline_max_drawdown_pct")
        if self.candidate_max_drawdown_pct < 0 or self.baseline_max_drawdown_pct < 0:
            raise ValueError("drawdown percentages must be non-negative")
        if self.orders_authorized is not False:
            raise ValueError("orders_authorized must be false")


@dataclass(frozen=True, slots=True)
class LearningComparisonReport:
    status: str
    promotion_status: str
    evaluation_sample_size: int
    candidate_net_return_pct: float
    baseline_net_return_pct: float
    candidate_max_drawdown_pct: float
    baseline_max_drawdown_pct: float
    evaluation_dataset_fingerprint: str
    orders_authorized: bool = False


def build_research_dataset(
    experiments: Iterable[StrategyResearchExperiment],
) -> ResearchDatasetSnapshot:
    """Build a learning snapshot from approved validation folds only.

    Untouched final holdout metrics are deliberately excluded from training rows.
    """
    items = tuple(experiments)
    if not items:
        raise ValueError("at least one validated research experiment is required")
    rows: list[dict[str, Any]] = []
    ids: list[str] = []
    for experiment in items:
        if not isinstance(experiment, StrategyResearchExperiment):
            raise ValueError("learning data must come from StrategyResearchExperiment records")
        if experiment.status != "EVALUATED" or experiment.orders_authorized is not False:
            raise ValueError("only evaluated, paper-only experiments may enter learning datasets")
        outcomes = experiment.outcomes
        holdout = outcomes.get("untouched_holdout")
        if (
            outcomes.get("holdout_used_for_selection") is not False
            or not isinstance(holdout, dict)
            or holdout.get("used_for_selection") is not False
        ):
            raise ValueError("final holdout must remain untouched")
        folds = outcomes.get("validation_folds")
        if not isinstance(folds, list) or not folds:
            raise ValueError("validated experiment must contain out-of-sample validation folds")
        ids.append(experiment.experiment_id)
        for index, fold in enumerate(folds):
            if not isinstance(fold, dict):
                raise ValueError("validation fold evidence is malformed")
            strategy_return = _finite(fold.get("strategy_net_return_pct"), "strategy_net_return_pct")
            benchmark_return = _finite(fold.get("buy_hold_return_pct"), "buy_hold_return_pct")
            row = {
                "experiment_id": experiment.experiment_id,
                "candidate_fingerprint": experiment.candidate_fingerprint,
                "source_provenance_id": experiment.source_provenance_id,
                "experiment_version": experiment.experiment_version,
                "fold_index": index,
                "strategy_net_return_pct": strategy_return,
                "buy_hold_return_pct": benchmark_return,
                "excess_return_pct": strategy_return - benchmark_return,
            }
            rows.append(row)
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate experiment IDs are not allowed in a learning dataset")
    ordered_ids = tuple(sorted(ids))
    canonical_rows = sorted(rows, key=lambda row: (row["experiment_id"], row["fold_index"]))
    material = json.dumps(
        {"experiment_ids": ordered_ids, "validation_rows": canonical_rows},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    fingerprint = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return ResearchDatasetSnapshot(
        dataset_fingerprint=fingerprint,
        source_experiment_ids=ordered_ids,
        validation_rows=tuple(canonical_rows),
        orders_authorized=False,
    )


def validate_learning_proposal(
    proposal: ResearchLearningProposal,
    dataset: ResearchDatasetSnapshot,
) -> LearningProposalValidation:
    if not isinstance(proposal, ResearchLearningProposal):
        raise ValueError("proposal must be a ResearchLearningProposal")
    if not isinstance(dataset, ResearchDatasetSnapshot) or dataset.orders_authorized is not False:
        raise ValueError("a paper-only research dataset is required")
    if proposal.dataset_fingerprint != dataset.dataset_fingerprint:
        raise ValueError("learning proposal references a different dataset")
    if not set(proposal.source_experiment_ids).issubset(set(dataset.source_experiment_ids)):
        raise ValueError("learning proposal references experiments outside its dataset")
    return LearningProposalValidation(
        status="RESEARCH_ONLY",
        allowed_experiment_ids=dataset.source_experiment_ids,
        orders_authorized=False,
    )


def compare_learning_proposal(
    proposal: ResearchLearningProposal,
    dataset: ResearchDatasetSnapshot,
    independent_evaluation: IndependentLearningEvaluation,
    *,
    minimum_sample_size: int = 30,
) -> LearningComparisonReport:
    """Compare a research proposal with a fixed baseline on independent data.

    A positive comparison only earns human review; promotion is never automatic.
    """
    validate_learning_proposal(proposal, dataset)
    if not isinstance(independent_evaluation, IndependentLearningEvaluation):
        raise ValueError("independent evaluation evidence is required")
    if independent_evaluation.orders_authorized is not False:
        raise ValueError("orders_authorized must be false")
    if independent_evaluation.evaluation_dataset_fingerprint == dataset.dataset_fingerprint:
        raise ValueError("evaluation data must be independent of the learning dataset")
    if isinstance(minimum_sample_size, bool) or not isinstance(minimum_sample_size, int) or minimum_sample_size < 1:
        raise ValueError("minimum_sample_size must be a positive integer")
    if independent_evaluation.sample_size < minimum_sample_size:
        status = "INSUFFICIENT_INDEPENDENT_EVIDENCE"
    elif (
        independent_evaluation.candidate_net_return_pct > independent_evaluation.baseline_net_return_pct
        and independent_evaluation.candidate_max_drawdown_pct
        <= independent_evaluation.baseline_max_drawdown_pct
    ):
        status = "REVIEW_REQUIRED"
    else:
        status = "REJECTED"
    return LearningComparisonReport(
        status=status,
        promotion_status="NOT_PROMOTED",
        evaluation_sample_size=independent_evaluation.sample_size,
        candidate_net_return_pct=independent_evaluation.candidate_net_return_pct,
        baseline_net_return_pct=independent_evaluation.baseline_net_return_pct,
        candidate_max_drawdown_pct=independent_evaluation.candidate_max_drawdown_pct,
        baseline_max_drawdown_pct=independent_evaluation.baseline_max_drawdown_pct,
        evaluation_dataset_fingerprint=independent_evaluation.evaluation_dataset_fingerprint,
        orders_authorized=False,
    )
