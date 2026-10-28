from datetime import datetime, timedelta, timezone

from backend.market_connectivity_check import run_public_connectivity_check
from backend.market_data_adapter import BinanceUsdMFuturesAdapter

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def make_transport(*, server_time=NOW, time_error=None):
    calls = []
    server_ms = int(server_time.timestamp() * 1000)

    def transport(path, params, timeout):
        calls.append((path, dict(params)))
        if path == "/fapi/v1/time":
            if time_error is not None:
                raise time_error
            return {"serverTime": server_ms}
        if path == "/fapi/v1/premiumIndex":
            return {
                "symbol": "BTCUSDT", "markPrice": "100.1", "indexPrice": "100.0",
                "lastFundingRate": "0.0001", "nextFundingTime": server_ms + 28_800_000,
                "time": server_ms,
            }
        if path == "/fapi/v1/klines":
            return [
                [server_ms - 180000, "99", "101", "98", "100", "10", server_ms - 120001, "1000", 20, "4", "400", "0"],
                [server_ms - 120000, "100", "102", "99", "101", "11", server_ms - 60001, "1100", 22, "5", "500", "0"],
                [server_ms - 60000, "101", "102", "100", "100.1", "12", server_ms - 1, "1200", 24, "6", "600", "0"],
            ]
        raise AssertionError(f"unexpected endpoint {path}")

    return transport, calls


def test_public_connectivity_check_verifies_time_mark_and_closed_candles():
    transport, calls = make_transport()
    adapter = BinanceUsdMFuturesAdapter(testnet=True, transport=transport)

    report = run_public_connectivity_check(adapter, "BTCUSDT", clock=lambda: NOW)

    assert report.passed is True
    assert report.orders_authorized is False
    assert report.base_url == "https://demo-fapi.binance.com"
    assert report.symbol == "BTCUSDT"
    assert [endpoint.name for endpoint in report.endpoints] == ["server_time", "mark_price", "closed_klines"]
    assert all(endpoint.ok for endpoint in report.endpoints)
    assert report.closed_candle_count >= 1
    assert [path for path, _ in calls] == ["/fapi/v1/time", "/fapi/v1/premiumIndex", "/fapi/v1/klines"]
    assert all(params.get("symbol", "BTCUSDT") == "BTCUSDT" for _, params in calls)


def test_public_connectivity_check_reports_network_failure_without_raising():
    transport, calls = make_transport(time_error=OSError("network unavailable"))
    report = run_public_connectivity_check(
        BinanceUsdMFuturesAdapter(testnet=True, transport=transport), "BTCUSDT", clock=lambda: NOW,
    )

    assert report.passed is False
    assert report.endpoints[0].name == "server_time"
    assert report.endpoints[0].ok is False
    assert len(calls) == 1
    assert report.orders_authorized is False


def test_public_connectivity_check_marks_excessive_clock_skew_as_failed():
    transport, _ = make_transport()
    local_clock = lambda: NOW - timedelta(seconds=10)
    report = run_public_connectivity_check(
        BinanceUsdMFuturesAdapter(testnet=True, transport=transport), "BTCUSDT",
        clock=local_clock, max_clock_skew_ms=1000,
    )

    assert report.clock_skew_ok is False
    assert report.passed is False
    assert all(endpoint.ok for endpoint in report.endpoints)


def test_public_connectivity_check_rejects_non_uppercase_symbol():
    transport, calls = make_transport()
    report = run_public_connectivity_check(
        BinanceUsdMFuturesAdapter(testnet=True, transport=transport), "btcusdt", clock=lambda: NOW,
    )
    assert report.passed is False
    assert "uppercase" in report.error.lower()
    assert calls == []
