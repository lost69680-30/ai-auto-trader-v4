import math

import pytest

from backend.market_relationship_graph import build_market_relationship_graph


def test_relationship_graph_finds_positive_and_inverse_correlations():
    returns = {
        "BTCUSDT": [0.01, 0.02, -0.01, 0.03, 0.00, -0.02],
        "ETHUSDT": [0.02, 0.04, -0.02, 0.06, 0.00, -0.04],
        "XRPUSDT": [-0.01, -0.02, 0.01, -0.03, 0.00, 0.02],
    }

    graph = build_market_relationship_graph(returns, correlation_threshold=0.95, min_observations=6)

    assert len(graph.nodes) == 3
    assert len(graph.edges) == 3
    by_pair = {(edge.source, edge.target): edge for edge in graph.edges}
    assert by_pair[("BTCUSDT", "ETHUSDT")].relationship == "POSITIVE"
    assert by_pair[("BTCUSDT", "XRPUSDT")].relationship == "INVERSE"
    assert by_pair[("BTCUSDT", "ETHUSDT")].correlation == pytest.approx(1.0)
    assert all(node.degree == 2 for node in graph.nodes)
    assert graph.orders_authorized is False


def test_graph_is_deterministic_independent_of_input_mapping_order():
    a = {"BTCUSDT": [1, 2, 3, 4], "ETHUSDT": [2, 4, 6, 8]}
    b = {"ETHUSDT": [2, 4, 6, 8], "BTCUSDT": [1, 2, 3, 4]}

    first = build_market_relationship_graph(a, correlation_threshold=0.9, min_observations=4)
    second = build_market_relationship_graph(b, correlation_threshold=0.9, min_observations=4)

    assert first == second
    assert first.input_fingerprint == second.input_fingerprint


def test_graph_rejects_gapped_series_with_different_lengths():
    with pytest.raises(ValueError, match="same number of observations"):
        build_market_relationship_graph(
            {"BTCUSDT": [1, 2, 3, 4], "ETHUSDT": [1, 2, 3]},
            correlation_threshold=0.5,
            min_observations=3,
        )


def test_graph_rejects_non_finite_returns():
    with pytest.raises(ValueError, match="finite"):
        build_market_relationship_graph(
            {"BTCUSDT": [1, 2, math.nan], "ETHUSDT": [2, 3, 4]},
            correlation_threshold=0.5,
            min_observations=3,
        )


def test_graph_rejects_short_histories_and_invalid_thresholds():
    with pytest.raises(ValueError, match="minimum observation"):
        build_market_relationship_graph(
            {"BTCUSDT": [1, 2], "ETHUSDT": [2, 3]},
            correlation_threshold=0.5,
            min_observations=3,
        )
    with pytest.raises(ValueError, match="correlation_threshold"):
        build_market_relationship_graph(
            {"BTCUSDT": [1, 2, 3], "ETHUSDT": [2, 3, 4]},
            correlation_threshold=1.5,
            min_observations=3,
        )


def test_constant_series_produces_nodes_without_fabricated_edges():
    graph = build_market_relationship_graph(
        {"BTCUSDT": [0.01] * 6, "ETHUSDT": [0.02] * 6},
        correlation_threshold=0.5,
        min_observations=6,
    )
    assert len(graph.nodes) == 2
    assert graph.edges == ()
