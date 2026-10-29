from datetime import datetime, timedelta, timezone

import pytest

from backend.market_data_adapter import MarketCandle, MarketUniverse
from backend.scanner import Contract, MarketSnapshot, ScannerConfig
from backend.spot_market_research import (
    SpotMarketResearchService,
    SpotResearchConfig,
)


NOW = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)


def sample_candles(symbol="BTCUSDT"):
    values = [
        (100, 102, 99, 101, 100),
        (101, 103, 100, 102, 100),
        (102, 104, 101, 103, 100),
        (103, 106, 102, 105.5, 180),
    ]
    rows = []
    for index, (opened, high, low, close, volume) in enumerate(values):
        open_time = NOW - timedelta(minutes=5 * (len(values) - index))
        close_time = open_time + timedelta(minutes=5) - timedelta(milliseconds=1)
        rows.append(MarketCandle(
            symbol=symbol, open_time=open_time, close_time=close_time,
            open=opened, high=high, low=low, close=close, volume=volume,
            quote_volume=volume * close, trades=10, is_closed=True,
        ))
    return tuple(rows)


class FakeSpotSource:
    def __init__(self, *, contract_type="SPOT", candles=None):
        self.contract_type = contract_type
        self.candles = candles if candles is not None else sample_candles()
        self.klines_calls = []

    def fetch_market_universe(self, *, now=None):
        contract = Contract(
            "BTCUSDT", "TRADING", self.contract_type, "USDT",
            tick_size=0.01, quantity_step=0.001, min_quantity=0.001,
            min_notional=5.0,
        )
        snapshot = MarketSnapshot(
            "BTCUSDT", NOW, 105.5, 5_000_000.0, 3.0, 5.0
        )
        return MarketUniverse((contract,), (snapshot,), NOW, orders_authorized=False)

    def fetch_klines(self, symbol, *, interval="1m", limit=100, now=None):
        self.klines_calls.append((symbol, interval, limit))
        return self.candles


def config():
    return SpotResearchConfig(
        interval="5m",
        candle_limit=20,
        max_candle_age_seconds=120,
        lookback_candles=2,
        scanner=ScannerConfig(
            required_contract_type="SPOT",
            min_quote_volume_24h=1_000_000,
            max_spread_bps=10,
            min_volatility_pct=0.05,
            max_volatility_pct=15,
        ),
    )


def test_spot_service_returns_review_only_ohlcv_opportunity():
    source = FakeSpotSource()
    report = SpotMarketResearchService(source, config()).analyze_once(now=NOW)
    assert report.status == "REVIEW_CANDIDATES"
    assert len(report.opportunities) == 1
    item = report.opportunities[0]
    assert item.symbol == "BTCUSDT"
    assert item.direction == "LONG"
    assert "BREAKOUT_UP" in item.patterns
    assert any("no funding data used" in evidence.lower() for evidence in item.evidence)
    assert item.orders_authorized is False
    assert report.orders_authorized is False
    assert source.klines_calls == [("BTCUSDT", "5m", 20)]


def test_spot_service_rejects_futures_contracts():
    source = FakeSpotSource(contract_type="PERPETUAL")
    report = SpotMarketResearchService(source, config()).analyze_once(now=NOW)
    assert report.opportunities == ()
    assert report.orders_authorized is False
    assert source.klines_calls == []


def test_spot_service_rejects_candle_gaps_fail_closed():
    rows = list(sample_candles())
    rows[-1] = MarketCandle(
        symbol=rows[-1].symbol,
        open_time=rows[-1].open_time + timedelta(minutes=5),
        close_time=rows[-1].close_time + timedelta(minutes=5),
        open=rows[-1].open, high=rows[-1].high, low=rows[-1].low,
        close=rows[-1].close, volume=rows[-1].volume,
        quote_volume=rows[-1].quote_volume, trades=rows[-1].trades, is_closed=True,
    )
    report = SpotMarketResearchService(
        FakeSpotSource(candles=tuple(rows)), config()
    ).analyze_once(now=NOW)
    assert report.opportunities == ()
    assert any("gapped" in rejection.reason for rejection in report.rejections)


def test_spot_config_refuses_futures_scanner():
    with pytest.raises(ValueError, match="required_contract_type"):
        SpotResearchConfig(scanner=ScannerConfig(required_contract_type="PERPETUAL"))


def test_spot_service_rejects_naive_clock():
    with pytest.raises(ValueError, match="timezone-aware"):
        SpotMarketResearchService(FakeSpotSource(), config()).analyze_once(
            now=datetime(2026, 10, 29, 12, 0)
        )
