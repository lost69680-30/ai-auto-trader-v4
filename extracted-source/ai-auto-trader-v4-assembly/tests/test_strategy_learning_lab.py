from datetime import datetime, timedelta, timezone

import pytest

from backend.market_data_lineage import MarketDataManifest
from backend.strategy_learning_lab import (
    IndependentLearningEvaluation,
    ResearchLearningProposal,
    build_research_dataset,
    compare_learning_proposal,
)
from backend.strategy_research_memory import StrategyResearchExperiment


UTC = timezone.utc


def _manifest(start_day: int, end_day: int, digest: str) -> dict:
    return MarketDataManifest(
        venue="BINANCE_USDM_TESTNET",
        market_type="FUTURES",
        symbol="BTCUSDT",
        interval="1m",
        start_timestamp=datetime(2026, 1, start_day, tzinfo=UTC),
        end_timestamp=datetime(2026, 1, end_day, tzinfo=UTC),
        row_count=3000,
        source_id="binance-public-testnet",
        sha256=digest * 64,
        orders_authorized=False,
    ).to_dict()


def _qualified_experiment() -> StrategyResearchExperiment:
    folds = []
    for index in range(4):
        folds.append({
            "name": f"validation_{index + 1}",
            "strategy_net_return_pct": 1.2,
            "buy_hold_return_pct": 0.2,
            "trade_count": 8,
            "max_drawdown_pct": 1.5,
            "market_regime": {
                "trend_state": "UP",
                "volatility_state": "LOW",
                "trend_return_pct": 1.0,
                "realized_volatility_pct": 0.1,
                "observations": 500,
                "methodology": "closed-candle trend and realized-volatility thresholds",
                "orders_authorized": False,
            },
        })
    outcomes = {
        "validation_folds": folds,
        "untouched_holdout": {
            "net_pnl": 999999.0,
            "trade_count": 999,
            "used_for_selection": False,
            "orders_authorized": False,
        },
        "holdout_used_for_selection": False,
        "validation_quality": {
            "status": "EVALUATED",
            "fold_count": 4,
            "total_validation_trades": 32,
            "mean_excess_return_pct": 1.0,
            "positive_fold_pct": 100.0,
            "maximum_fold_drawdown_pct": 1.5,
            "failure_reasons": [],
            "holdout_used": False,
            "orders_authorized": False,
        },
        "orders_authorized": False,
    }
    return StrategyResearchExperiment(
        experiment_id="exp-qualified-1",
        candidate_id="solo-trend-sma-cross:BTCUSDT",
        candidate_fingerprint="candidate-fingerprint-1",
        source_provenance_id="source-provenance-1",
        experiment_version=1,
        dataset_start=datetime(2026, 1, 1, tzinfo=UTC),
        dataset_end=datetime(2026, 1, 5, tzinfo=UTC),
        configuration_version="research-v1",
        assumptions={"market_data_manifest": _manifest(1, 5, "a")},
        outcomes=outcomes,
        status="EVALUATED",
        failure_reasons=(),
        orders_authorized=False,
    )


def _proposal(dataset) -> ResearchLearningProposal:
    return ResearchLearningProposal(
        proposal_id="proposal-ppo-1",
        agent_id="offline-ppo-research",
        agent_kind="PPO",
        dataset_fingerprint=dataset.dataset_fingerprint,
        source_experiment_ids=dataset.source_experiment_ids,
        rationale="Offline research proposal using validation-only features.",
        weights={"indicator.ema_fast": 0.25, "indicator.rsi": 0.75},
        orders_authorized=False,
    )


def _independent_evaluation(*, sample_size: int = 40, start_day: int = 6):
    manifest = MarketDataManifest(
        venue="BINANCE_USDM_TESTNET",
        market_type="FUTURES",
        symbol="BTCUSDT",
        interval="1m",
        start_timestamp=datetime(2026, 1, start_day, tzinfo=UTC),
        end_timestamp=datetime(2026, 1, start_day + 4, tzinfo=UTC),
        row_count=3000,
        source_id="binance-public-testnet",
        sha256="b" * 64,
        orders_authorized=False,
    )
    return IndependentLearningEvaluation(
        evaluation_dataset_fingerprint="independent-evaluation-dataset-1",
        sample_size=sample_size,
        candidate_net_return_pct=2.0,
        baseline_net_return_pct=1.0,
        candidate_max_drawdown_pct=1.0,
        baseline_max_drawdown_pct=1.5,
        market_data_manifests=(manifest,),
        orders_authorized=False,
    )


def test_learning_dataset_contains_validation_folds_but_never_holdout_values():
    dataset = build_research_dataset([_qualified_experiment()])

    assert len(dataset.validation_rows) == 4
    assert all("untouched_holdout" not in row and "holdout" not in row for row in dataset.validation_rows)
    assert dataset.orders_authorized is False


def test_ppo_proposal_is_only_human_review_eligible_after_independent_evaluation():
    dataset = build_research_dataset([_qualified_experiment()])
    report = compare_learning_proposal(_proposal(dataset), dataset, _independent_evaluation())

    assert report.status == "REVIEW_REQUIRED"
    assert report.promotion_status == "NOT_PROMOTED"
    assert report.orders_authorized is False


def test_small_independent_sample_never_earns_review_status():
    dataset = build_research_dataset([_qualified_experiment()])
    report = compare_learning_proposal(
        _proposal(dataset), dataset, _independent_evaluation(sample_size=10)
    )

    assert report.status == "INSUFFICIENT_INDEPENDENT_EVIDENCE"
    assert report.promotion_status == "NOT_PROMOTED"


def test_learning_proposal_cannot_modify_risk_or_execution_weights():
    with pytest.raises(ValueError, match="cannot modify risk or execution"):
        ResearchLearningProposal(
            proposal_id="proposal-unsafe",
            agent_id="unsafe-agent",
            agent_kind="PPO",
            dataset_fingerprint="a" * 64,
            source_experiment_ids=("exp-qualified-1",),
            rationale="must be rejected",
            weights={"risk_fraction": 0.01},
            orders_authorized=False,
        )


def test_learning_dataset_rejects_insufficient_validation_quality():
    experiment = _qualified_experiment()
    outcomes = dict(experiment.outcomes)
    quality = dict(outcomes["validation_quality"])
    quality["failure_reasons"] = ["too few positive folds"]
    outcomes["validation_quality"] = quality
    rejected = StrategyResearchExperiment(
        experiment_id=experiment.experiment_id,
        candidate_id=experiment.candidate_id,
        candidate_fingerprint=experiment.candidate_fingerprint,
        source_provenance_id=experiment.source_provenance_id,
        experiment_version=experiment.experiment_version,
        dataset_start=experiment.dataset_start,
        dataset_end=experiment.dataset_end,
        configuration_version=experiment.configuration_version,
        assumptions=experiment.assumptions,
        outcomes=outcomes,
        status="EVALUATED",
        failure_reasons=(),
        orders_authorized=False,
    )
    with pytest.raises(ValueError, match="failure reasons"):
        build_research_dataset([rejected])


def test_independent_evaluation_rejects_overlapping_market_data_windows():
    dataset = build_research_dataset([_qualified_experiment()])
    with pytest.raises(ValueError, match="overlaps"):
        compare_learning_proposal(
            _proposal(dataset), dataset, _independent_evaluation(start_day=4)
        )
