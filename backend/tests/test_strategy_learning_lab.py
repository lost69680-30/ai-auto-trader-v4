from datetime import datetime, timedelta, timezone

import pytest

from backend.strategy_research_memory import StrategyResearchExperiment
from backend.market_data_lineage import MarketDataManifest
from backend.strategy_learning_lab import (
    IndependentLearningEvaluation,
    ResearchLearningProposal,
    build_research_dataset,
    compare_learning_proposal,
    validate_learning_proposal,
)

NOW = datetime(2026, 11, 2, 18, 0, tzinfo=timezone.utc)


def manifest(*, start=None, end=None, source_id="learning-source", digest="a" * 64):
    return MarketDataManifest(
        venue="BINANCE_USDM_TESTNET",
        market_type="FUTURES",
        symbol="BTCUSDT",
        interval="1m",
        start_timestamp=start or NOW - timedelta(days=90),
        end_timestamp=end or NOW - timedelta(days=1),
        row_count=100,
        source_id=source_id,
        sha256=digest,
        orders_authorized=False,
    )


def experiment(*, status="EVALUATED", holdout_used=False, nested_holdout_used=False):
    return StrategyResearchExperiment(
        experiment_id="exp-001",
        candidate_id="candidate-001",
        candidate_fingerprint="candidate-fingerprint-001",
        source_provenance_id="source-provenance-001",
        experiment_version=1,
        dataset_start=NOW - timedelta(days=90),
        dataset_end=NOW - timedelta(days=1),
        configuration_version="wf-v1",
        assumptions={"fee_bps_per_side": 4, "orders_authorized": False, "market_data_manifest": manifest().to_dict()},
        outcomes={
            "validation_folds": [
                {"strategy_net_return_pct": 2.0, "buy_hold_return_pct": 1.0,
                 "market_regime": {"trend_state": "UP", "volatility_state": "LOW", "trend_return_pct": 2.0, "realized_volatility_pct": 0.1, "observations": 39, "methodology": "test methodology", "orders_authorized": False}},
                {"strategy_net_return_pct": 3.0, "buy_hold_return_pct": 1.0,
                 "market_regime": {"trend_state": "DOWN", "volatility_state": "HIGH", "trend_return_pct": -2.0, "realized_volatility_pct": 1.2, "observations": 39, "methodology": "test methodology", "orders_authorized": False}},
            ],
            "untouched_holdout": {"strategy_net_return_pct": 1.0, "used_for_selection": nested_holdout_used},
            "holdout_used_for_selection": holdout_used,
            "orders_authorized": False,
        },
        status=status,
        orders_authorized=False,
    )


def proposal(dataset, *, weights=None, agent_kind="GAT", dataset_fingerprint=None):
    return ResearchLearningProposal(
        proposal_id="proposal-001",
        agent_id="research-agent-1",
        agent_kind=agent_kind,
        dataset_fingerprint=dataset_fingerprint or dataset.dataset_fingerprint,
        source_experiment_ids=dataset.source_experiment_ids,
        rationale="Candidate appears more robust across validation folds.",
        weights=weights or {"momentum": 0.6, "trend": 0.4},
        orders_authorized=False,
    )


def test_learning_dataset_uses_only_validated_folds_and_excludes_holdout():
    snapshot = build_research_dataset([experiment()])

    assert snapshot.source_experiment_ids == ("exp-001",)
    assert len(snapshot.validation_rows) == 2
    assert "untouched_holdout" not in str(snapshot.validation_rows)
    assert snapshot.validation_rows[0]["market_regime"]["trend_state"] == "UP"
    assert snapshot.orders_authorized is False


@pytest.mark.parametrize(
    "item",
    [
        experiment(status="DRIFT_FLAGGED"),
        experiment(holdout_used=True),
        experiment(nested_holdout_used=True),
    ],
)
def test_learning_dataset_rejects_unapproved_or_leaked_experiments(item):
    with pytest.raises(ValueError):
        build_research_dataset([item])


def test_learning_proposal_must_be_bound_to_its_validated_dataset():
    snapshot = build_research_dataset([experiment()])
    candidate = proposal(snapshot, agent_kind="PPO")
    assert validate_learning_proposal(candidate, snapshot).status == "RESEARCH_ONLY"

    with pytest.raises(ValueError, match="dataset"):
        validate_learning_proposal(
            proposal(snapshot, dataset_fingerprint="different-dataset"),
            snapshot,
        )


@pytest.mark.parametrize(
    "weights",
    [
        {"max_leverage": 5.0},
        {"daily_loss_limit": 0.5},
        {"orders_authorized": 1.0},
    ],
)
def test_learning_agents_cannot_propose_risk_setting_changes(weights):
    snapshot = build_research_dataset([experiment()])
    with pytest.raises(ValueError, match="risk"):
        proposal(snapshot, weights=weights)


def test_learning_proposal_requires_independent_evaluation_and_never_promotes():
    snapshot = build_research_dataset([experiment()])
    candidate = proposal(snapshot)
    independent = IndependentLearningEvaluation(
        evaluation_dataset_fingerprint="independent-holdout-v2",
        sample_size=100,
        candidate_net_return_pct=4.0,
        baseline_net_return_pct=3.0,
        candidate_max_drawdown_pct=5.0,
        baseline_max_drawdown_pct=5.5,
        market_data_manifests=(manifest(start=NOW, end=NOW + timedelta(hours=1), source_id="holdout-source", digest="b" * 64),),
        orders_authorized=False,
    )

    report = compare_learning_proposal(candidate, snapshot, independent)

    assert report.status == "REVIEW_REQUIRED"
    assert report.promotion_status == "NOT_PROMOTED"
    assert report.orders_authorized is False


def test_learning_comparison_rejects_reused_or_insufficient_evaluation_data():
    snapshot = build_research_dataset([experiment()])
    candidate = proposal(snapshot)
    reused = IndependentLearningEvaluation(
        evaluation_dataset_fingerprint=snapshot.dataset_fingerprint,
        sample_size=100,
        candidate_net_return_pct=5.0,
        baseline_net_return_pct=3.0,
        candidate_max_drawdown_pct=4.0,
        baseline_max_drawdown_pct=5.0,
        market_data_manifests=(manifest(),),
        orders_authorized=False,
    )
    with pytest.raises(ValueError, match="independent"):
        compare_learning_proposal(candidate, snapshot, reused)

    insufficient = IndependentLearningEvaluation(
        evaluation_dataset_fingerprint="independent-holdout-v3",
        sample_size=5,
        candidate_net_return_pct=5.0,
        baseline_net_return_pct=3.0,
        candidate_max_drawdown_pct=4.0,
        baseline_max_drawdown_pct=5.0,
        market_data_manifests=(manifest(start=NOW, end=NOW + timedelta(hours=1), source_id="small-holdout", digest="c" * 64),),
        orders_authorized=False,
    )
    report = compare_learning_proposal(candidate, snapshot, insufficient, minimum_sample_size=30)
    assert report.status == "INSUFFICIENT_INDEPENDENT_EVIDENCE"
    assert report.promotion_status == "NOT_PROMOTED"


@pytest.mark.parametrize(
    "weights",
    [
        {"capital_exposure": 0.2},
        {"max_drawdown_pct": 0.1},
        {"margin_mode": 1.0},
        {"equity_reserve": 0.1},
        {"notional_cap": 100.0},
    ],
)
def test_learning_agents_cannot_smuggle_portfolio_controls_as_model_weights(weights):
    snapshot = build_research_dataset([experiment()])
    with pytest.raises(ValueError, match="risk or execution"):
        proposal(snapshot, weights=weights)


def test_learning_dataset_rows_and_proposal_weights_are_immutable():
    from dataclasses import FrozenInstanceError

    snapshot = build_research_dataset([experiment()])
    candidate = proposal(snapshot)
    original_value = snapshot.validation_rows[0]["strategy_net_return_pct"]

    with pytest.raises(TypeError):
        snapshot.validation_rows[0]["strategy_net_return_pct"] = 999.0
    with pytest.raises(TypeError):
        candidate.weights["momentum"] = 99.0

    assert snapshot.validation_rows[0]["strategy_net_return_pct"] == original_value
