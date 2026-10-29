from datetime import datetime, timezone

import pytest

from backend.market_data_adapter import BinanceUsdMFuturesAdapter, MarketDataError


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def make_transport(payloads):
    calls = []
    def transport(path, params, timeout):
        calls.append((path, dict(params), timeout))
        value = payloads[path]
        if isinstance(value, Exception):
            raise value
        return value
    return transport, calls


def test_market_universe_joins_contract_ticker_and_order_book_without_credentials():
    payloads = {
        "/fapi/v1/exchangeInfo": {"symbols": [
            {"symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
            {"symbol": "ABCUSDT", "status": "BREAK", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
        ]},
        "/fapi/v1/ticker/24hr": [
            {"symbol": "BTCUSDT", "lastPrice": "100", "quoteVolume": "2000000", "highPrice": "105", "lowPrice": "95"},
            {"symbol": "ABCUSDT", "lastPrice": "10", "quoteVolume": "100000", "highPrice": "11", "lowPrice": "9"},
        ],
        "/fapi/v1/ticker/bookTicker": [
            {"symbol": "BTCUSDT", "bidPrice": "99.9", "askPrice": "100.1"},
            {"symbol": "ABCUSDT", "bidPrice": "9.9", "askPrice": "10.1"},
        ],
    }
    transport, calls = make_transport(payloads)
    adapter = BinanceUsdMFuturesAdapter(transport=transport)

    universe = adapter.fetch_market_universe(now=NOW)

    assert [c.symbol for c in universe.contracts] == ["BTCUSDT", "ABCUSDT"]
    btc = next(s for s in universe.snapshots if s.symbol == "BTCUSDT")
    assert btc.last_price == 100
    assert btc.quote_volume_24h == 2_000_000
    assert btc.spread_bps == pytest.approx(20)
    assert btc.volatility_pct == pytest.approx(10)
    assert btc.observed_at == NOW
    assert universe.orders_authorized is False
    assert len(calls) == 3
    assert all(call[2] > 0 for call in calls)


def test_market_universe_fails_closed_when_one_required_public_endpoint_fails():
    transport, _ = make_transport({
        "/fapi/v1/exchangeInfo": {"symbols": []},
        "/fapi/v1/ticker/24hr": RuntimeError("network down"),
        "/fapi/v1/ticker/bookTicker": [],
    })
    adapter = BinanceUsdMFuturesAdapter(transport=transport)

    with pytest.raises(MarketDataError):
        adapter.fetch_market_universe(now=NOW)


def test_invalid_order_book_and_nonfinite_ticker_are_not_emitted():
    payloads = {
        "/fapi/v1/exchangeInfo": {"symbols": [
            {"symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
            {"symbol": "ETHUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
        ]},
        "/fapi/v1/ticker/24hr": [
            {"symbol": "BTCUSDT", "lastPrice": "NaN", "quoteVolume": "10", "highPrice": "11", "lowPrice": "9"},
            {"symbol": "ETHUSDT", "lastPrice": "100", "quoteVolume": "1000", "highPrice": "101", "lowPrice": "99"},
        ],
        "/fapi/v1/ticker/bookTicker": [
            {"symbol": "BTCUSDT", "bidPrice": "99", "askPrice": "101"},
            {"symbol": "ETHUSDT", "bidPrice": "101", "askPrice": "100"},
        ],
    }
    transport, _ = make_transport(payloads)
    universe = BinanceUsdMFuturesAdapter(transport=transport).fetch_market_universe(now=NOW)

    assert universe.snapshots == ()


def test_kline_parser_uses_exchange_timestamps_and_marks_open_candle_not_closed():
    start = int(NOW.timestamp() * 1000)
    payloads = {"/fapi/v1/klines": [
        [start - 120000, "99", "102", "98", "101", "12", start - 60001, "1200", 17, "5", "500", "0"],
        [start - 30000, "101", "103", "100", "102", "14", start + 29999, "1400", 19, "6", "600", "0"],
    ]}
    transport, calls = make_transport(payloads)
    candles = BinanceUsdMFuturesAdapter(transport=transport).fetch_klines(
        "BTCUSDT", interval="1m", limit=10, now=NOW
    )

    assert len(candles) == 2
    assert candles[0].open == 99
    assert candles[0].trades == 17
    assert candles[0].is_closed is True
    assert candles[1].is_closed is False
    assert calls[0][1] == {"symbol": "BTCUSDT", "interval": "1m", "limit": 10}


def test_kline_parser_rejects_bad_ohlc_and_invalid_limits():
    transport, _ = make_transport({"/fapi/v1/klines": [
        [1, "100", "99", "98", "100", "1", 2, "100", 1, "0", "0", "0"]
    ]})
    adapter = BinanceUsdMFuturesAdapter(transport=transport)
    with pytest.raises(MarketDataError):
        adapter.fetch_klines("BTCUSDT", interval="1m", limit=1, now=NOW)
    with pytest.raises(ValueError):
        adapter.fetch_klines("BTCUSDT", interval="1m", limit=0, now=NOW)
    with pytest.raises(ValueError):
        adapter.fetch_klines("BTCUSDT", interval="20m", limit=10, now=NOW)


def test_mark_price_endpoint_returns_mark_index_and_funding_context():
    observed_ms = int(NOW.timestamp() * 1000)
    transport, calls = make_transport({"/fapi/v1/premiumIndex": {
        "symbol": "BTCUSDT", "markPrice": "100.5", "indexPrice": "100.4",
        "lastFundingRate": "0.0001", "nextFundingTime": observed_ms + 8 * 60 * 60 * 1000,
        "time": observed_ms,
    }})
    adapter = BinanceUsdMFuturesAdapter(testnet=True, transport=transport)

    mark = adapter.fetch_mark_price("BTCUSDT")

    assert adapter.base_url == "https://demo-fapi.binance.com"
    assert mark.mark_price == 100.5
    assert mark.index_price == 100.4
    assert mark.last_funding_rate == pytest.approx(0.0001)
    assert mark.observed_at == NOW
    assert mark.orders_authorized is False
    assert calls[0][0] == "/fapi/v1/premiumIndex"
    assert calls[0][1] == {"symbol": "BTCUSDT"}


def test_mark_price_malformed_payload_fails_closed():
    transport, _ = make_transport({"/fapi/v1/premiumIndex": {
        "symbol": "BTCUSDT", "markPrice": "NaN", "indexPrice": "100.4",
        "lastFundingRate": "0.0001", "nextFundingTime": int(NOW.timestamp() * 1000),
        "time": int(NOW.timestamp() * 1000),
    }})
    with pytest.raises(MarketDataError):
        BinanceUsdMFuturesAdapter(transport=transport).fetch_mark_price("BTCUSDT")


def test_market_universe_parses_exchange_contract_price_and_quantity_filters():
    payloads = {
        "/fapi/v1/exchangeInfo": {"symbols": [{
            "symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "100"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
        }]},
        "/fapi/v1/ticker/24hr": [{
            "symbol": "BTCUSDT", "lastPrice": "100", "quoteVolume": "2000000", "highPrice": "105", "lowPrice": "95",
        }],
        "/fapi/v1/ticker/bookTicker": [{"symbol": "BTCUSDT", "bidPrice": "99.9", "askPrice": "100.1"}],
    }
    transport, _ = make_transport(payloads)

    universe = BinanceUsdMFuturesAdapter(transport=transport).fetch_market_universe(now=NOW)

    contract = universe.contracts[0]
    assert contract.tick_size == pytest.approx(0.1)
    assert contract.quantity_step == pytest.approx(0.001)
    assert contract.min_quantity == pytest.approx(0.001)
    assert contract.max_quantity == pytest.approx(100)
    assert contract.min_notional == pytest.approx(5)


def test_market_universe_does_not_guess_missing_contract_filters():
    payloads = {
        "/fapi/v1/exchangeInfo": {"symbols": [{
            "symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT",
        }]},
        "/fapi/v1/ticker/24hr": [{
            "symbol": "BTCUSDT", "lastPrice": "100", "quoteVolume": "2000000", "highPrice": "105", "lowPrice": "95",
        }],
        "/fapi/v1/ticker/bookTicker": [{"symbol": "BTCUSDT", "bidPrice": "99.9", "askPrice": "100.1"}],
    }
    transport, _ = make_transport(payloads)

    contract = BinanceUsdMFuturesAdapter(transport=transport).fetch_market_universe(now=NOW).contracts[0]

    assert contract.tick_size is None
    assert contract.quantity_step is None
    assert contract.min_quantity is None


def test_duplicate_exchange_filter_types_do_not_create_ambiguous_contract_metadata():
    payloads = {
        "/fapi/v1/exchangeInfo": {"symbols": [{
            "symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
            ],
        }]},
        "/fapi/v1/ticker/24hr": [{
            "symbol": "BTCUSDT", "lastPrice": "100", "quoteVolume": "2000000", "highPrice": "105", "lowPrice": "95",
        }],
        "/fapi/v1/ticker/bookTicker": [{"symbol": "BTCUSDT", "bidPrice": "99.9", "askPrice": "100.1"}],
    }
    transport, _ = make_transport(payloads)

    contract = BinanceUsdMFuturesAdapter(transport=transport).fetch_market_universe(now=NOW).contracts[0]

    assert contract.tick_size is None


def test_fetch_server_time_validates_public_time_endpoint():
    transport, calls = make_transport({"/fapi/v1/time": {"serverTime": int(NOW.timestamp() * 1000)}})
    adapter = BinanceUsdMFuturesAdapter(testnet=True, transport=transport)

    assert adapter.fetch_server_time() == NOW
    assert calls[0][0] == "/fapi/v1/time"
    assert adapter.base_url == "https://demo-fapi.binance.com"


def test_fetch_server_time_rejects_malformed_response():
    transport, _ = make_transport({"/fapi/v1/time": {"serverTime": "not-a-time"}})
    with pytest.raises(MarketDataError):
        BinanceUsdMFuturesAdapter(transport=transport).fetch_server_time()

@pytest.mark.parametrize("invalid", [True, False, -1, 0, None, "not-a-time", float("nan")])
def test_fetch_server_time_rejects_nonpositive_or_invalid_epoch_values(invalid):
    transport, _ = make_transport({"/fapi/v1/time": {"serverTime": invalid}})
    with pytest.raises(MarketDataError):
        BinanceUsdMFuturesAdapter(transport=transport).fetch_server_time()


def test_public_funding_history_is_sorted_validated_and_read_only():
    server_ms = int(NOW.timestamp() * 1000)
    def transport(path, params, timeout):
        assert path == "/fapi/v1/fundingRate"
        assert params["symbol"] == "BTCUSDT"
        return [
            {"symbol": "BTCUSDT", "fundingRate": "0.0002", "fundingTime": server_ms + 8_000},
            {"symbol": "BTCUSDT", "fundingRate": "-0.0001", "fundingTime": server_ms, "markPrice": "100"},
        ]
    records = BinanceUsdMFuturesAdapter(transport=transport).fetch_funding_rate_history("BTCUSDT")
    assert len(records) == 2
    assert records[0].funding_rate == pytest.approx(-0.0001)
    assert records[0].mark_price == pytest.approx(100)
    assert records[0].orders_authorized is False


def test_public_funding_history_rejects_duplicate_or_mismatched_rows():
    stamp = int(NOW.timestamp() * 1000)
    def transport(path, params, timeout):
        return [
            {"symbol": "BTCUSDT", "fundingRate": "0.001", "fundingTime": stamp},
            {"symbol": "BTCUSDT", "fundingRate": "0.001", "fundingTime": stamp},
        ]
    with pytest.raises(Exception, match="duplicate"):
        BinanceUsdMFuturesAdapter(transport=transport).fetch_funding_rate_history("BTCUSDT")


def test_spot_market_universe_uses_spot_endpoints_and_spot_contract_metadata():
    from backend.market_data_adapter import BinanceSpotMarketDataAdapter

    payloads = {
        "/api/v3/exchangeInfo": {"symbols": [{
            "symbol": "BTCUSDT", "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "100"},
                {"filterType": "NOTIONAL", "minNotional": "5"},
            ],
        }]},
        "/api/v3/ticker/24hr": [{
            "symbol": "BTCUSDT", "lastPrice": "100", "quoteVolume": "2000000",
            "highPrice": "105", "lowPrice": "95",
        }],
        "/api/v3/ticker/bookTicker": [{"symbol": "BTCUSDT", "bidPrice": "99.9", "askPrice": "100.1"}],
    }
    transport, calls = make_transport(payloads)
    adapter = BinanceSpotMarketDataAdapter(transport=transport)

    universe = adapter.fetch_market_universe(now=NOW)

    assert adapter.base_url == "https://data-api.binance.vision"
    assert [call[0] for call in calls] == [
        "/api/v3/exchangeInfo", "/api/v3/ticker/24hr", "/api/v3/ticker/bookTicker",
    ]
    assert universe.orders_authorized is False
    assert universe.contracts[0].contract_type == "SPOT"
    assert universe.contracts[0].tick_size == pytest.approx(0.1)
    assert universe.contracts[0].quantity_step == pytest.approx(0.001)
    assert universe.contracts[0].min_notional == pytest.approx(5)
    assert universe.snapshots[0].symbol == "BTCUSDT"
    assert universe.snapshots[0].last_price == pytest.approx(100)


def test_spot_server_time_uses_spot_time_endpoint():
    from backend.market_data_adapter import BinanceSpotMarketDataAdapter

    transport, calls = make_transport({"/api/v3/time": {"serverTime": int(NOW.timestamp() * 1000)}})
    adapter = BinanceSpotMarketDataAdapter(transport=transport)

    assert adapter.fetch_server_time() == NOW
    assert calls[0][0] == "/api/v3/time"
    assert adapter.base_url == "https://data-api.binance.vision"


def test_spot_funding_history_is_explicitly_unsupported():
    from backend.market_data_adapter import BinanceSpotMarketDataAdapter

    def transport(path, params, timeout):
        raise AssertionError("Spot funding-history call must not hit a Futures endpoint")

    adapter = BinanceSpotMarketDataAdapter(transport=transport)
    with pytest.raises(NotImplementedError, match="Spot markets do not have funding rates"):
        adapter.fetch_funding_rate_history("BTCUSDT")
