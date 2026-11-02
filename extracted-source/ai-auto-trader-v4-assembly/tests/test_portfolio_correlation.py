import pytest

from app.engines.portfolio_correlation import (
    calculate_pairwise_correlations,
    summarize_portfolio_correlations,
)


def test_pairwise_correlation_uses_only_shared_timestamped_returns():
    report = calculate_pairwise_correlations({
        "BTCUSDT": {1: 0.01, 2: 0.02, 3: -0.01, 4: 0.03},
        "ETHUSDT": {1: 0.02, 2: 0.04, 3: -0.02, 4: 0.06},
        "SOLUSDT": {1: -0.01, 2: 0.01, 3: 0.02, 4: -0.02},
    }, min_observations=4)

    btc_eth = report.pairs[0]
    assert (btc_eth.symbol_a, btc_eth.symbol_b) == ("BTCUSDT", "ETHUSDT")
    assert btc_eth.observations == 4
    assert btc_eth.correlation == pytest.approx(1.0)
    assert report.orders_authorized is False


def test_pairwise_correlation_does_not_fill_missing_timestamps():
    report = calculate_pairwise_correlations({
        "BTCUSDT": {1: 0.01, 2: 0.02, 3: -0.01, 4: 0.03},
        "ETHUSDT": {1: 0.02, 2: 0.04, 5: -0.02, 6: 0.06},
    }, min_observations=3)

    assert report.pairs[0].observations == 2
    assert report.pairs[0].correlation is None
    assert report.pairs[0].status == "INSUFFICIENT_OVERLAP"


def test_constant_return_series_has_undefined_correlation():
    report = calculate_pairwise_correlations({
        "BTCUSDT": {1: 0.01, 2: 0.01, 3: 0.01},
        "ETHUSDT": {1: 0.02, 2: 0.03, 3: 0.04},
    }, min_observations=3)

    assert report.pairs[0].correlation is None
    assert report.pairs[0].status == "ZERO_VARIANCE"


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), True, "0.1"])
def test_non_finite_or_non_numeric_returns_are_rejected(bad_value):
    with pytest.raises(ValueError, match="finite numeric"):
        calculate_pairwise_correlations({
            "BTCUSDT": {1: 0.01, 2: 0.02},
            "ETHUSDT": {1: bad_value, 2: 0.03},
        }, min_observations=2)


def test_invalid_symbol_or_timestamp_is_rejected():
    with pytest.raises(ValueError, match="symbol"):
        calculate_pairwise_correlations({"btc": {1: 0.1}, "ETHUSDT": {1: 0.2}})
    with pytest.raises(ValueError, match="timestamp"):
        calculate_pairwise_correlations({"BTCUSDT": {-1: 0.1}, "ETHUSDT": {-1: 0.2}})


def test_single_asset_report_has_no_pairs():
    report = calculate_pairwise_correlations({"BTCUSDT": {1: 0.1, 2: -0.1}})
    assert report.pairs == ()
    assert report.orders_authorized is False

def test_extreme_finite_returns_produce_stable_finite_correlation():
    report = calculate_pairwise_correlations({
        "BTCUSDT": {1: 1e308, 2: -1e308, 3: 1e308},
        "ETHUSDT": {1: -1e308, 2: 1e308, 3: -1e308},
    }, min_observations=3)

    assert report.pairs[0].correlation == pytest.approx(-1.0)
    assert report.pairs[0].status == "OK"
    assert report.orders_authorized is False


def test_portfolio_correlation_summary_reports_coverage_without_imputing_unknown_pairs():
    report = calculate_pairwise_correlations({
        "BTCUSDT": {1: 0.01, 2: 0.02, 3: -0.01, 4: 0.03},
        "ETHUSDT": {1: 0.02, 2: 0.04, 3: -0.02, 4: 0.06},
        "SOLUSDT": {1: -0.01, 2: 0.01, 5: 0.02, 6: -0.02},
    }, min_observations=3)

    summary = summarize_portfolio_correlations(report, high_correlation_threshold=0.9)

    assert summary.pair_count == 3
    assert summary.defined_pair_count == 1
    assert summary.undefined_pair_count == 2
    assert summary.coverage_pct == pytest.approx(100 / 3)
    assert summary.max_abs_correlation == pytest.approx(1.0)
    assert len(summary.high_correlation_pairs) == 1
    assert summary.orders_authorized is False


def test_single_asset_correlation_summary_has_unknown_coverage_not_zero():
    report = calculate_pairwise_correlations({"BTCUSDT": {1: 0.1, 2: -0.1}})
    summary = summarize_portfolio_correlations(report, high_correlation_threshold=0.9)

    assert summary.pair_count == 0
    assert summary.coverage_pct is None
    assert summary.max_abs_correlation is None
    assert summary.high_correlation_pairs == ()
    assert summary.orders_authorized is False


@pytest.mark.parametrize("threshold", [True, -0.1, 1.1, 10 ** 1000])
def test_portfolio_correlation_summary_rejects_invalid_thresholds(threshold):
    report = calculate_pairwise_correlations({
        "BTCUSDT": {1: 0.01, 2: 0.02},
        "ETHUSDT": {1: 0.02, 2: 0.04},
    }, min_observations=2)

    with pytest.raises(ValueError, match="threshold"):
        summarize_portfolio_correlations(report, high_correlation_threshold=threshold)
