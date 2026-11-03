from datetime import datetime, timezone

import pytest

from backend.market_data_adapter import BinanceSpotMarketDataAdapter, MarketDataError


NOW = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)


def test_spot_universe_uses_spot_public_endpoints_and_marks_spot_contracts():
    payloads = {
        "/api/v3/exchangeInfo": {"symbols": [{
            "symbol": "BTCUSDT", "status": "TRADING", "quoteAsset": "USDT",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001", "maxQty": "9000"},
                {"filterType": "NOTIONAL", "minNotional": "5"},
            ],
        }]},
        "/api/v3/ticker/24hr": [{
            "symbol": "BTCUSDT", "lastPrice": "100", "quoteVolume": "2000000",
            "highPrice": "105", "lowPrice": "95",
        }],
        "/api/v3/ticker/bookTicker": [{
            "symbol": "BTCUSDT", "bidPrice": "99.9", "askPrice": "100.1",
        }],
    }
    calls = []

    def transport(path, params, timeout):
        calls.append(path)
        return payloads[path]

    adapter = BinanceSpotMarketDataAdapter(transport=transport)
    universe = adapter.fetch_market_universe(now=NOW)

    assert calls == [
        "/api/v3/exchangeInfo", "/api/v3/ticker/24hr", "/api/v3/ticker/bookTicker"
    ]
    assert len(universe.contracts) == 1
    contract = universe.contracts[0]
    assert (contract.symbol, contract.status, contract.contract_type, contract.quote_asset) == (
        "BTCUSDT", "TRADING", "SPOT", "USDT"
    )
    assert contract.tick_size == pytest.approx(0.01)
    assert contract.quantity_step == pytest.approx(0.00001)
    assert contract.min_notional == pytest.approx(5)
    snapshot = universe.snapshots[0]
    assert snapshot.last_price == pytest.approx(100)
    assert snapshot.quote_volume_24h == pytest.approx(2_000_000)
    assert snapshot.spread_bps == pytest.approx(20)
    assert universe.fetched_at == NOW
    assert universe.orders_authorized is False


def test_spot_universe_fails_closed_on_required_endpoint_error():
    def transport(path, params, timeout):
        if path == "/api/v3/exchangeInfo":
            return {"symbols": []}
        raise RuntimeError("endpoint unavailable")

    adapter = BinanceSpotMarketDataAdapter(transport=transport)
    with pytest.raises(MarketDataError):
        adapter.fetch_market_universe(now=NOW)