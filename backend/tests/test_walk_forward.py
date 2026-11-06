from datetime import datetime, timedelta, timezone

import pytest

from backend.strategy_research import Candle
from backend.dataset_lineage import DatasetProvenance
from backend.walk_forward import WalkForwardConfig, evaluate_walk_forward

NOW = datetime(2026, 10, 10, 7, 0, tzinfo=timezone.utc)


def make_candles(n=240, *, drift=0.05):
    start = NOW - timedelta(minutes=n - 1)
    rows = []
    price = 100.0
    for i in range(n):
        price *= 1.0 + drift / 100.0
        rows.append(Candle("BTCUSDT", start + timedelta(minutes=i), price, price * 1.002,
                           price * 0.998, price, 1000.0))
    return rows


def test_walk_forward_has_chronological_disjoint_folds_and_untouched_holdout():
    rows = make_candles(360)
    result = evaluate_walk_forward(
        "BTCUSDT", rows,
        config=WalkForwardConfig(train_bars=120, validation_bars=40, holdout_bars=40,
                                 step_bars=40, candle_interval_minutes=1),
        research_now=NOW, dataset_provider="binance-public", dataset_venue="binance-spot-public",
    )
    assert result.orders_authorized is False
    assert result.holdout is not None
    assert result.holdout.start_index == len(rows) - 40
    assert all(fold.start_index < fold.end_index for fold in result.folds)
    assert all(a.end_index <= b.start_index for a, b in zip(result.folds, result.folds[1:]))
    assert result.holdout.start_index >= max(f.end_index for f in result.folds)
    assert result.holdout.used_for_selection is False


def test_walk_forward_reports_buy_hold_and_cash_baselines():
    result = evaluate_walk_forward(
        "BTCUSDT", make_candles(360),
        config=WalkForwardConfig(train_bars=120, validation_bars=40, holdout_bars=40,
                                 step_bars=40),
        research_now=NOW, dataset_provider="binance-public", dataset_venue="binance-spot-public",
    )
    assert result.folds
    for fold in result.folds:
        assert fold.buy_hold_return_pct is not None
        assert fold.cash_return_pct == 0.0
        assert fold.strategy.orders_authorized is False
    assert result.holdout.buy_hold_return_pct is not None
    assert result.holdout.cash_return_pct == 0.0


def test_walk_forward_rejects_short_or_irregular_data_without_trades():
    rows = make_candles(199)
    with pytest.raises(ValueError, match="not enough"):
        evaluate_walk_forward("BTCUSDT", rows,
            config=WalkForwardConfig(train_bars=120, validation_bars=40, holdout_bars=40, step_bars=40),
            research_now=NOW, dataset_provider="binance-public", dataset_venue="binance-spot-public")
    rows = make_candles(360)
    rows[200] = Candle(rows[200].symbol, rows[200].timestamp + timedelta(minutes=1),
                       rows[200].open, rows[200].high, rows[200].low, rows[200].close, rows[200].volume)
    with pytest.raises(ValueError, match="interval"):
        evaluate_walk_forward("BTCUSDT", rows,
            config=WalkForwardConfig(train_bars=120, validation_bars=40, holdout_bars=40, step_bars=40),
            research_now=NOW, dataset_provider="binance-public", dataset_venue="binance-spot-public")


def test_walk_forward_rejects_holdout_smaller_than_minimum():
    with pytest.raises(ValueError, match="holdout"):
        WalkForwardConfig(train_bars=120, validation_bars=40, holdout_bars=5, step_bars=40)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"train_bars": 120.5, "validation_bars": 40, "holdout_bars": 40, "step_bars": 40},
        {"train_bars": 120, "validation_bars": 40.5, "holdout_bars": 40, "step_bars": 40},
        {"train_bars": 120, "validation_bars": 40, "holdout_bars": 40, "step_bars": True},
        {"train_bars": 120, "validation_bars": 40, "holdout_bars": 40, "step_bars": 40, "candle_interval_minutes": 1.5},
    ],
)
def test_walk_forward_rejects_non_integer_fold_geometry(kwargs):
    with pytest.raises(ValueError, match="integer"):
        WalkForwardConfig(**kwargs)


def test_walk_forward_rejects_overlapping_out_of_sample_windows():
    with pytest.raises(ValueError, match="step_bars"):
        WalkForwardConfig(train_bars=120, validation_bars=40, holdout_bars=40, step_bars=20)


def test_walk_forward_rejects_candles_after_requested_research_clock():
    rows = [
        Candle(row.symbol, row.timestamp + timedelta(minutes=1), row.open, row.high,
               row.low, row.close, row.volume)
        for row in make_candles(360)
    ]
    with pytest.raises(ValueError, match="future"):
        evaluate_walk_forward(
            "BTCUSDT", rows,
            config=WalkForwardConfig(train_bars=120, validation_bars=40, holdout_bars=40),
            research_now=NOW, dataset_provider="binance-public", dataset_venue="binance-spot-public",
        )


def test_walk_forward_returns_content_addressed_dataset_provenance():
    rows = make_candles(360)
    result = evaluate_walk_forward(
        "BTCUSDT", rows,
        config=WalkForwardConfig(train_bars=120, validation_bars=40, holdout_bars=40, step_bars=40),
        research_now=NOW, dataset_provider="binance-public", dataset_venue="spot",
    )
    provenance = result.dataset_provenance
    assert isinstance(provenance, DatasetProvenance)
    assert provenance.provider == "binance-public"
    assert provenance.venue == "spot"
    assert provenance.symbol == "BTCUSDT"
    assert provenance.timeframe == "1m"
    assert provenance.candle_count == len(rows)
    assert len(provenance.data_sha256) == 64
    assert result.orders_authorized is False


def test_walk_forward_rejects_overlapping_reference_dataset_before_evaluation():
    from backend.dataset_lineage import candle_dataset_sha256
    rows = make_candles(360)
    reference = DatasetProvenance(
        provider="binance-public", venue="spot", symbol="BTCUSDT", timeframe="1m",
        window_start=rows[100].timestamp, window_end=rows[200].timestamp,
        candle_count=101, data_sha256="f" * 64,
    )
    with pytest.raises(ValueError, match="windows overlap"):
        evaluate_walk_forward(
            "BTCUSDT", rows,
            config=WalkForwardConfig(train_bars=120, validation_bars=40, holdout_bars=40, step_bars=40),
            research_now=NOW, dataset_provider="binance-public", dataset_venue="spot",
            reference_datasets=(reference,),
        )



def test_walk_forward_reports_descriptive_market_regime_for_each_window():
    rows = make_candles(360, drift=0.05)
    result = evaluate_walk_forward(
        "BTCUSDT", rows,
        config=WalkForwardConfig(train_bars=120, validation_bars=40, holdout_bars=40, step_bars=40),
        research_now=NOW,
    )
    assert result.folds
    for fold in result.folds:
        assert fold.market_regime.trend_state == "UP"
        assert fold.market_regime.volatility_state == "LOW"
        assert fold.market_regime.observations == 39
        assert fold.market_regime.orders_authorized is False
    assert result.holdout.market_regime.trend_state == "UP"
    assert result.holdout.market_regime.orders_authorized is False


def test_buy_hold_baseline_uses_same_equity_exposure_fraction_as_strategy():
    from backend.backtesting import BacktestConfig

    rows = make_candles(360, drift=0.05)
    backtest_config = BacktestConfig(position_fraction=0.25)
    result = evaluate_walk_forward(
        "BTCUSDT", rows,
        config=WalkForwardConfig(train_bars=120, validation_bars=40, holdout_bars=40, step_bars=40),
        backtest_config=backtest_config,
        research_now=NOW,
    )
    fold = result.folds[0]
    test_rows = rows[fold.start_index:fold.end_index]
    gross_return = test_rows[-1].close / test_rows[0].open - 1.0
    round_trip_cost = 2.0 * (
        backtest_config.fee_bps_per_side + backtest_config.slippage_bps_per_side
    ) / 10_000.0
    expected = (gross_return - round_trip_cost) * backtest_config.position_fraction * 100.0
    assert fold.buy_hold_return_pct == pytest.approx(expected, abs=0.000001)
