from datetime import datetime, timezone

from backend.market_data_adapter import BinanceSpotMarketDataAdapter

NOW = datetime(2026, 10, 24, 15, 0, tzinfo=timezone.utc)


def test_spot_adapter_uses_documented_spot_public_endpoints_and_keeps_orders_disabled():
    now_ms = int(NOW.timestamp() * 1000)
    calls = []
    def transport(path, params, timeout):
        calls.append((path, dict(params)))
        if path == "/api/v3/klines":
            return [[now_ms - 120000, "100", "103", "99", "102", "10", now_ms - 60001, "1000", 20, "5", "500", "0"]]
        if path == "/api/v3/ticker/24hr":
            return {"symbol": "BTCUSDT", "lastPrice": "102", "closeTime": now_ms - 1000}
        raise AssertionError(path)

    adapter = BinanceSpotMarketDataAdapter(transport=transport)
    candles = adapter.fetch_klines("BTCUSDT", interval="1m", limit=2, now=NOW)
    price = adapter.fetch_spot_price("BTCUSDT")

    assert candles[0].is_closed is True
    assert candles[0].close == 102
    assert price.last_price == 102
    assert price.orders_authorized is False
    assert [path for path, _ in calls] == ["/api/v3/klines", "/api/v3/ticker/24hr"]
    assert all(path.startswith("/api/v3/") for path, _ in calls)


def test_spot_last_price_observation_time_uses_local_receive_time_not_24h_window_close_time():
    from datetime import datetime as dt
    before = dt.now(timezone.utc)
    adapter = BinanceSpotMarketDataAdapter(transport=lambda path, params, timeout: {
        "symbol": "BTCUSDT", "lastPrice": "102",
        # Observation time must use local receive time, not the 24-hour window close.
        "closeTime": int((before.timestamp() + 300) * 1000),
    })
    price = adapter.fetch_spot_price("BTCUSDT")
    after = dt.now(timezone.utc)

    assert before <= price.observed_at <= after
    assert price.orders_authorized is False
