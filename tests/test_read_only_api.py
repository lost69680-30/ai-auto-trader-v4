import json
import io
from datetime import datetime, timedelta, timezone

import pytest

from app.engines.paper_ledger import SQLitePaperLedger
from app.engines.read_only_api import create_app

TOKEN = "unit-test-token-that-is-at-least-32-characters-long"


def request(app, path="/api/v1/status", method="GET", auth=None, payload=None):
    captured = {}
    def start(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)
    route, separator, query = path.partition("?")
    environ = {"REQUEST_METHOD": method, "PATH_INFO": route, "QUERY_STRING": query if separator else "",
               "HTTP_AUTHORIZATION": auth or "", "CONTENT_LENGTH": str(len(payload or b"")), "wsgi.input": io.BytesIO(payload or b"")}
    body = b"".join(app(environ, start))
    return captured["status"], captured["headers"], json.loads(body)


def test_api_requires_strong_token_and_reads_status(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass
    app = create_app(database_path=db, token=TOKEN)
    status, _, body = request(app)
    assert status.startswith("401")
    status, headers, body = request(app, auth="Bearer " + TOKEN)
    assert status.startswith("200")
    assert body["mode"] == "PAPER_ONLY"
    assert body["orders_authorized"] is False
    assert headers["Cache-Control"] == "no-store"


def test_api_has_no_mutation_routes_and_rejects_non_get(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass
    app = create_app(database_path=db, token=TOKEN)
    status, _, body = request(app, path="/api/v1/orders", method="POST", auth="Bearer " + TOKEN)
    assert status.startswith("405")
    assert body["orders_authorized"] is False
    status, _, body = request(app, path="/api/v1/place-order", auth="Bearer " + TOKEN)
    assert status.startswith("404")


def test_api_refuses_short_tokens_and_memory_database():
    with pytest.raises(ValueError):
        create_app(database_path="./ledger.sqlite3", token="short")
    with pytest.raises(ValueError):
        create_app(database_path=":memory:", token=TOKEN)


def test_markets_endpoint_exposes_only_validated_read_only_snapshots(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass
    app = create_app(
        database_path=db,
        token=TOKEN,
        market_status=lambda: {"status": "HEALTHY", "checked_at": 123.0, "orders_authorized": False},
        market_items=lambda: [{"symbol": "BTCUSDT", "mark_price": 65000.0, "orders_authorized": False}],
    )
    status, _, body = request(app, path="/api/v1/markets", auth="Bearer " + TOKEN)
    assert status.startswith("200")
    assert body["status"] == "HEALTHY"
    assert body["items"][0]["symbol"] == "BTCUSDT"
    assert body["orders_authorized"] is False


def test_markets_endpoint_fails_closed_when_feed_is_not_healthy(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass
    app = create_app(
        database_path=db,
        token=TOKEN,
        market_status=lambda: {"status": "OFFLINE", "reason": "upstream unavailable", "orders_authorized": False},
        market_items=lambda: [],
    )
    status, _, body = request(app, path="/api/v1/markets", auth="Bearer " + TOKEN)
    assert status.startswith("503")
    assert body["items"] == []
    assert body["orders_authorized"] is False


def test_authenticated_emergency_stop_post_activates_persistent_paper_stop(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass
    app = create_app(database_path=db, token=TOKEN)
    payload = json.dumps({"reason": "user requested emergency stop"}).encode()
    status, _, body = request(app, path="/api/v1/emergency-stop", method="POST", auth="Bearer " + TOKEN, payload=payload)
    assert status.startswith("200")
    assert body["emergency_stop_active"] is True
    assert body["orders_authorized"] is False
    status, _, status_body = request(app, auth="Bearer " + TOKEN)
    assert status.startswith("200")
    assert status_body["emergency_stop_active"] is True


def test_emergency_stop_rejects_missing_auth_and_invalid_reason(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass
    app = create_app(database_path=db, token=TOKEN)
    payload = json.dumps({"reason": "   "}).encode()
    status, _, _ = request(app, path="/api/v1/emergency-stop", method="POST", payload=payload)
    assert status.startswith("401")
    status, _, body = request(app, path="/api/v1/emergency-stop", method="POST", auth="Bearer " + TOKEN, payload=payload)
    assert status.startswith("400")
    assert body["orders_authorized"] is False


def test_emergency_stop_rejects_malformed_or_oversized_payload(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass
    app = create_app(database_path=db, token=TOKEN)
    for payload in (b"{bad", b"x" * 2049):
        status, _, body = request(app, path="/api/v1/emergency-stop", method="POST", auth="Bearer " + TOKEN, payload=payload)
        assert status.startswith("400")
        assert body["orders_authorized"] is False


def test_other_mutation_routes_remain_blocked(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass
    app = create_app(database_path=db, token=TOKEN)
    status, _, body = request(app, path="/api/v1/orders", method="POST", auth="Bearer " + TOKEN)
    assert status.startswith("405")
    assert body["orders_authorized"] is False


def test_candle_history_endpoint_returns_direct_interval_data_without_order_authority(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass
    candle = {"open_time": "2026-10-28T00:00:00+00:00", "close_time": "2026-10-28T00:00:59+00:00",
              "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 100.0, "is_closed": True}
    app = create_app(database_path=db, token=TOKEN,
                     market_candles=lambda symbol: {"symbol": symbol, "candles_by_interval": {"1m": [candle], "5m": [candle]}, "orders_authorized": False})
    status, _, body = request(app, path="/api/v1/markets/candles?symbol=BTCUSDT", auth="Bearer " + TOKEN)
    assert status.startswith("200")
    assert body["symbol"] == "BTCUSDT"
    assert body["candles_by_interval"]["5m"][0]["is_closed"] is True
    assert body["orders_authorized"] is False


def test_candle_history_endpoint_rejects_missing_or_invalid_symbol(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass
    app = create_app(database_path=db, token=TOKEN,
                     market_candles=lambda symbol: {"symbol": symbol, "candles_by_interval": {}, "orders_authorized": False})
    status, _, body = request(app, path="/api/v1/markets/candles?symbol=", auth="Bearer " + TOKEN)
    assert status.startswith("400")
    assert body["orders_authorized"] is False


def test_binance_spot_testnet_account_endpoint_is_read_only_and_fails_closed(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass

    app = create_app(
        database_path=db,
        token=TOKEN,
        account_status=lambda: {
            "connected": True,
            "mode": "BINANCE_SPOT_TESTNET_READ_ONLY",
            "status": "CONNECTED",
            "balances": [{"asset": "USDT", "free": "100.0", "locked": "0.0"}],
            "orders_authorized": False,
        },
    )
    status, _, body = request(
        app, path="/api/v1/binance-spot-testnet/account", auth="Bearer " + TOKEN
    )
    assert status.startswith("200")
    assert body["mode"] == "BINANCE_SPOT_TESTNET_READ_ONLY"
    assert body["balances"] == [{"asset": "USDT", "free": "100.0", "locked": "0.0"}]
    assert body["orders_authorized"] is False

    unsafe_app = create_app(
        database_path=db,
        token=TOKEN,
        account_status=lambda: {
            "connected": True,
            "mode": "BINANCE_SPOT_TESTNET_READ_ONLY",
            "status": "CONNECTED",
            "balances": [{"asset": "USDT", "free": "100.0", "locked": "0.0"}],
            "orders_authorized": True,
        },
    )
    unsafe_status, _, unsafe_body = request(
        unsafe_app, path="/api/v1/binance-spot-testnet/account", auth="Bearer " + TOKEN
    )
    assert unsafe_status.startswith("503")
    assert unsafe_body["orders_authorized"] is False
    assert unsafe_body["balances"] == []


def test_status_reports_ephemeral_or_unverified_paper_state_storage(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass
    app = create_app(database_path=db, token=TOKEN)

    status, _, body = request(app, auth="Bearer " + TOKEN)

    assert status.startswith("200")
    storage = body["state_storage"]
    assert storage["durability"] == "EPHEMERAL"
    assert storage["survives_restart"] is False
    assert "restart" in storage["warning"].lower()
    assert body["orders_authorized"] is False


def test_emergency_stop_response_warns_when_storage_is_ephemeral(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass
    app = create_app(database_path=db, token=TOKEN)
    payload = json.dumps({"reason": "durability warning test"}).encode()

    status, _, body = request(
        app, path="/api/v1/emergency-stop", method="POST", auth="Bearer " + TOKEN, payload=payload,
    )

    assert status.startswith("200")
    assert body["emergency_stop_active"] is True
    assert body["state_storage"]["durability"] == "EPHEMERAL"
    assert body["state_storage"]["survives_restart"] is False
    assert body["orders_authorized"] is False


def test_candle_interval_fetches_run_concurrently_and_keep_failed_interval_isolated():
    import threading
    from app.engines.read_only_api import _fetch_candle_intervals

    barrier = threading.Barrier(2, timeout=2)

    class Row:
        open_time = datetime.fromisoformat("2026-10-28T00:00:00+00:00")
        close_time = datetime.fromisoformat("2026-10-28T00:00:59+00:00")
        open = 10.0
        high = 11.0
        low = 9.0
        close = 10.5
        volume = 100.0
        is_closed = True

    class Source:
        def fetch_klines(self, symbol, *, interval, limit):
            if interval in ("1m", "5m"):
                barrier.wait()
            if interval == "5m":
                raise RuntimeError("upstream interval failure")
            return [Row()]

    payloads, statuses, rows_by_interval = _fetch_candle_intervals(
        Source(), "BTCUSDT", ("1m", "5m", "1h"), 100
    )

    assert statuses == {"1m": "HEALTHY", "5m": "UNAVAILABLE:RuntimeError", "1h": "HEALTHY"}
    assert len(payloads["1m"]) == 1
    assert payloads["5m"] == []
    assert rows_by_interval["5m"] == ()
    assert len(payloads["1h"]) == 1


def test_status_discloses_that_full_research_to_paper_runtime_is_not_started(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass
    app = create_app(database_path=db, token=TOKEN)

    status, _, body = request(app, auth="Bearer " + TOKEN)

    runtime = body["analysis_runtime"]
    assert status.startswith("200")
    assert runtime["status"] == "READ_ONLY_MARKET_DATA_ONLY"
    assert runtime["research_pipeline"] == "NOT_STARTED"
    assert runtime["paper_simulation"] == "NOT_STARTED"
    assert runtime["orders_authorized"] is False
    assert "not started" in runtime["reason"].lower()



def test_api_exposes_injected_analysis_runtime_status(tmp_path):
    from app.engines.read_only_api import create_app

    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass
    expected = {
        "enabled": True,
        "status": "RUNNING",
        "research_pipeline": "RUNNING",
        "paper_simulation": "RUNNING",
        "last_cycle_at": "2026-10-29T12:00:00+00:00",
        "last_cycle_error": None,
        "orders_authorized": False,
        "reason": "OK",
    }
    app = create_app(database_path=db, token=TOKEN, analysis_status=lambda: expected)
    status, _, body = request(app, auth="Bearer " + TOKEN)
    assert status.startswith("200")
    assert body["analysis_runtime"] == expected


def test_analysis_runtime_health_only_reports_running_after_a_cycle():
    from types import SimpleNamespace
    from app.engines.read_only_api import AnalysisRuntimeHealth

    health = AnalysisRuntimeHealth(enabled=True)
    initial = health.status()
    assert initial["status"] == "STARTING"
    assert initial["last_cycle_at"] is None
    assert initial["orders_authorized"] is False

    report = SimpleNamespace(
        checked_at=1793275200.0,
        analysis_result=SimpleNamespace(status="OK"),
        analysis_status="HEALTHY",
    )
    health.publish(report)
    completed = health.status()
    assert completed["status"] == "RUNNING"
    assert completed["research_pipeline"] == "RUNNING"
    assert completed["paper_simulation"] == "RUNNING"
    assert completed["last_cycle_at"] is not None
    assert completed["last_cycle_error"] is None
    assert completed["orders_authorized"] is False

    health.fail(RuntimeError("sensitive internal detail"))
    failed = health.status()
    assert failed["status"] == "ERROR"
    assert failed["last_cycle_error"] == "RuntimeError"
    assert "sensitive internal detail" not in str(failed)
    assert failed["orders_authorized"] is False


def test_analysis_runtime_health_does_not_report_running_after_failed_analysis():
    from types import SimpleNamespace
    from app.engines.read_only_api import AnalysisRuntimeHealth

    health = AnalysisRuntimeHealth(enabled=True)
    report = SimpleNamespace(
        checked_at=1793275200.0,
        analysis_result=None,
        analysis_status="ANALYSIS_FAILED:ValueError",
    )

    health.publish(report)

    status = health.status()
    assert status["status"] == "ERROR"
    assert status["research_pipeline"] == "ERROR"
    assert status["paper_simulation"] == "ERROR"
    assert status["last_cycle_error"] == "ValueError"
    assert status["orders_authorized"] is False


def test_analysis_runtime_health_marks_unhealthy_market_cycle_degraded():
    from types import SimpleNamespace
    from app.engines.read_only_api import AnalysisRuntimeHealth

    health = AnalysisRuntimeHealth(enabled=True)
    report = SimpleNamespace(
        checked_at=1793275200.0,
        analysis_result=None,
        analysis_status="SKIPPED_UNHEALTHY_MARKET_DATA",
    )

    health.publish(report)

    status = health.status()
    assert status["status"] == "DEGRADED"
    assert status["research_pipeline"] == "DEGRADED"
    assert status["paper_simulation"] == "DEGRADED"
    assert status["last_cycle_error"] is None
    assert status["orders_authorized"] is False


def test_analysis_runtime_health_reports_funding_sync_degraded_and_error():
    from types import SimpleNamespace
    from app.engines.read_only_api import AnalysisRuntimeHealth

    degraded = AnalysisRuntimeHealth(enabled=True)
    degraded.publish(SimpleNamespace(
        checked_at=1793275200.0,
        analysis_result=None,
        analysis_status="FUNDING_SYNC_DEGRADED",
    ))
    assert degraded.status()["status"] == "DEGRADED"
    assert degraded.status()["orders_authorized"] is False

    failed = AnalysisRuntimeHealth(enabled=True)
    failed.publish(SimpleNamespace(
        checked_at=1793275200.0,
        analysis_result=None,
        analysis_status="FUNDING_SYNC_ERROR",
    ))
    assert failed.status()["status"] == "ERROR"
    assert failed.status()["last_cycle_error"] == "FundingSyncError"
    assert failed.status()["orders_authorized"] is False


def test_analysis_runtime_health_maps_market_universe_failure_and_empty_eligibility():
    from types import SimpleNamespace
    from app.engines.read_only_api import AnalysisRuntimeHealth

    failed = AnalysisRuntimeHealth(enabled=True)
    failed.publish(SimpleNamespace(
        checked_at=1793275200.0, analysis_result=None,
        analysis_status="MARKET_UNIVERSE_UNAVAILABLE",
    ))
    assert failed.status()["status"] == "ERROR"
    assert failed.status()["last_cycle_error"] == "MarketUniverseUnavailable"
    assert failed.status()["orders_authorized"] is False

    degraded = AnalysisRuntimeHealth(enabled=True)
    degraded.publish(SimpleNamespace(
        checked_at=1793275200.0, analysis_result=None,
        analysis_status="NO_ELIGIBLE_SYMBOLS",
    ))
    assert degraded.status()["status"] == "DEGRADED"
    assert degraded.status()["orders_authorized"] is False


def test_read_only_api_requires_valid_explicit_testnet_boolean_value():
    from app.engines.read_only_api import _parse_market_testnet

    assert _parse_market_testnet("true") is True
    assert _parse_market_testnet("yes") is True
    assert _parse_market_testnet("1") is True
    assert _parse_market_testnet("false") is False
    assert _parse_market_testnet("no") is False
    assert _parse_market_testnet("0") is False
    with pytest.raises(ValueError, match="PAPER_MARKET_TESTNET"):
        _parse_market_testnet("truue")


def test_audit_endpoint_returns_empty_list_for_empty_sqlite_ledger(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass
    app = create_app(database_path=db, token=TOKEN)

    status, _, body = request(app, path="/api/v1/audit", auth="Bearer " + TOKEN)

    assert status.startswith("200")
    assert body["items"] == []
    assert body["orders_authorized"] is False



def _closed_monthly_candles(year=2025):
    from backend.market_data_adapter import MarketCandle

    rows = []
    for month in range(1, 13):
        opened = datetime(year, month, 1, tzinfo=timezone.utc)
        next_month = (
            datetime(year + 1, 1, 1, tzinfo=timezone.utc)
            if month == 12 else datetime(year, month + 1, 1, tzinfo=timezone.utc)
        )
        rows.append(MarketCandle(
            symbol="BTCUSDT",
            open_time=opened,
            close_time=next_month - timedelta(milliseconds=1),
            open=100.0 + month,
            high=120.0 + month,
            low=90.0 + month,
            close=110.0 + month,
            volume=10.0 * month,
            quote_volume=1000.0 * month,
            trades=month,
            is_closed=True,
        ))
    return rows


def test_yearly_candle_api_assembles_only_complete_closed_months_and_status():
    from app.engines.read_only_api import build_market_candle_payload

    class Source:
        def fetch_klines(self, symbol, *, interval, limit):
            if interval == "1M":
                return _closed_monthly_candles()
            return []

    payload = build_market_candle_payload(
        source=Source(),
        symbol="BTCUSDT",
        allowed_symbols=("BTCUSDT",),
        intervals=("1M", "1d"),
        limit=100,
    )

    yearly = payload["candles_by_interval"]["1y"]
    assert payload["symbol"] == "BTCUSDT"
    assert payload["interval_status"]["1M"] == "HEALTHY"
    assert payload["interval_status"]["1y"] == "HEALTHY"
    assert len(yearly) == 1
    assert yearly[0]["open"] == 101.0
    assert yearly[0]["high"] == 132.0
    assert yearly[0]["low"] == 91.0
    assert yearly[0]["close"] == 122.0
    assert yearly[0]["volume"] == 780.0
    assert yearly[0]["is_closed"] is True
    assert payload["orders_authorized"] is False
    assert payload["mode"] == "PAPER_ONLY"


def test_yearly_candle_api_reports_monthly_fetch_failure_without_fabricating_data():
    from app.engines.read_only_api import build_market_candle_payload

    class Source:
        def fetch_klines(self, symbol, *, interval, limit):
            if interval == "1M":
                raise RuntimeError("monthly history unavailable")
            return []

    payload = build_market_candle_payload(
        source=Source(),
        symbol="BTCUSDT",
        allowed_symbols=("BTCUSDT",),
        intervals=("1M", "1d"),
        limit=100,
    )

    assert payload["candles_by_interval"]["1y"] == []
    assert payload["interval_status"]["1M"] == "UNAVAILABLE:RuntimeError"
    assert payload["interval_status"]["1y"] == "UNAVAILABLE:MONTHLY_INTERVAL"
    assert payload["orders_authorized"] is False


def test_candle_api_contract_reports_all_native_intervals_and_isolates_failure():
    from types import SimpleNamespace
    from app.engines.read_only_api import build_market_candle_payload

    native_intervals = ("1m", "5m", "15m", "1h", "4h", "1d", "1w", "1M")

    class Source:
        def fetch_klines(self, symbol, *, interval, limit):
            if interval == "15m":
                raise RuntimeError("temporary interval outage")
            if interval == "1M":
                return _closed_monthly_candles(year=2024)
            return [SimpleNamespace(
                open_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
                close_time=datetime(2024, 1, 1, 0, 0, 59, tzinfo=timezone.utc),
                open=100.0, high=105.0, low=95.0, close=102.0,
                volume=12.0, is_closed=True,
            )]

    payload = build_market_candle_payload(
        source=Source(),
        symbol="BTCUSDT",
        allowed_symbols=("BTCUSDT",),
        intervals=native_intervals,
        limit=100,
    )

    assert tuple(payload["candles_by_interval"]) == (*native_intervals, "1y")
    for interval in native_intervals:
        if interval == "15m":
            assert payload["interval_status"][interval] == "UNAVAILABLE:RuntimeError"
            assert payload["candles_by_interval"][interval] == []
        else:
            assert payload["interval_status"][interval] == "HEALTHY"
            assert len(payload["candles_by_interval"][interval]) == (12 if interval == "1M" else 1)

    assert payload["interval_status"]["1y"] == "HEALTHY"
    assert len(payload["candles_by_interval"]["1y"]) == 1
    assert payload["orders_authorized"] is False
    assert payload["mode"] == "PAPER_ONLY"


def test_archived_market_history_endpoint_is_authenticated_and_read_only(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass
    seen = {}

    def history(symbol, start, end, limit):
        seen.update(symbol=symbol, start=start, end=end, limit=limit)
        return {
            "symbol": symbol,
            "venue": "BINANCE_USDM_TESTNET",
            "market_type": "FUTURES",
            "interval": "1m",
            "candles": [{
                "open_time": "2026-10-31T03:40:00+00:00",
                "close_time": "2026-10-31T03:40:59.999000+00:00",
                "open": 100.0, "high": 102.0, "low": 99.0,
                "close": 101.0, "volume": 12.0, "is_closed": True,
            }],
            "orders_authorized": False,
        }

    app = create_app(database_path=db, token=TOKEN, market_history=history)
    path = ("/api/v1/markets/history?symbol=BTCUSDT"
            "&start=2026-10-31T03%3A00%3A00%2B00%3A00"
            "&end=2026-10-31T04%3A00%3A00%2B00%3A00&limit=250")
    status, _, body = request(app, path=path, auth="Bearer " + TOKEN)

    assert status.startswith("200")
    assert seen["symbol"] == "BTCUSDT"
    assert seen["start"].tzinfo is not None
    assert seen["end"].tzinfo is not None
    assert seen["limit"] == 250
    assert body["venue"] == "BINANCE_USDM_TESTNET"
    assert body["candles"][0]["is_closed"] is True
    assert body["orders_authorized"] is False


def test_archived_market_history_endpoint_rejects_invalid_query_without_calling_provider(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass
    calls = []

    def history(*args):
        calls.append(args)
        return {}

    app = create_app(database_path=db, token=TOKEN, market_history=history)
    invalid_paths = [
        "/api/v1/markets/history?symbol=BTCUSDT&start=not-a-date&end=2026-10-31T04%3A00%3A00Z",
        "/api/v1/markets/history?symbol=BTCUSDT&start=2026-10-31T04%3A00%3A00Z&end=2026-10-31T03%3A00%3A00Z",
        "/api/v1/markets/history?symbol=BTCUSDT&start=2026-10-31T03%3A00%3A00Z&end=2026-10-31T04%3A00%3A00Z&limit=0",
    ]
    for path in invalid_paths:
        status, _, body = request(app, path=path, auth="Bearer " + TOKEN)
        assert status.startswith("400")
        assert body["orders_authorized"] is False
    assert calls == []


def test_archived_market_history_endpoint_fails_closed_when_provider_is_unavailable_or_unsafe(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass
    app = create_app(database_path=db, token=TOKEN)
    path = ("/api/v1/markets/history?symbol=BTCUSDT"
            "&start=2026-10-31T03%3A00%3A00Z&end=2026-10-31T04%3A00%3A00Z")
    status, _, body = request(app, path=path, auth="Bearer " + TOKEN)
    assert status.startswith("503")
    assert body["orders_authorized"] is False

    unsafe = create_app(
        database_path=db, token=TOKEN,
        market_history=lambda *args: {
            "symbol": "BTCUSDT", "venue": "BINANCE_USDM_TESTNET",
            "market_type": "FUTURES", "interval": "1m",
            "candles": [], "orders_authorized": True,
        },
    )
    status, _, body = request(unsafe, path=path, auth="Bearer " + TOKEN)
    assert status.startswith("503")
    assert body["orders_authorized"] is False
    assert body["candles"] == []



def test_public_healthz_reports_process_liveness_without_authentication(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass
    app = create_app(database_path=db, token=TOKEN)

    status, headers, body = request(app, path="/healthz")

    assert status.startswith("200")
    assert body == {
        "status": "ok", "service": "solo-api", "mode": "PAPER_ONLY",
        "orders_authorized": False,
    }
    assert headers["Cache-Control"] == "no-store"


def test_public_readyz_requires_fresh_healthy_shared_worker(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass
    app = create_app(
        database_path=db, token=TOKEN,
        market_status=lambda: {
            "status": "HEALTHY", "worker_status": "RUNNING", "orders_authorized": False,
        },
    )

    status, _, body = request(app, path="/readyz")

    assert status.startswith("200")
    assert body["status"] == "ready"
    assert body["worker_status"] == "RUNNING"
    assert body["orders_authorized"] is False


def test_public_readyz_fails_closed_when_worker_is_stale_or_missing(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass
    app = create_app(
        database_path=db, token=TOKEN,
        market_status=lambda: {
            "status": "STALE", "worker_status": "STOPPED", "orders_authorized": False,
        },
    )

    status, _, body = request(app, path="/readyz")

    assert status.startswith("503")
    assert body["status"] == "not_ready"
    assert body["worker_status"] == "STOPPED"
    assert body["orders_authorized"] is False

def test_market_assessment_endpoint_exposes_only_authenticated_advisory_results(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass
    report = {
        "status": "HEALTHY",
        "assessed_at": "2026-10-31T12:00:00+00:00",
        "market_type": "FUTURES",
        "scan_id": "scan-123",
        "scan_job_status": "RUNNING",
        "scan_started_at": "2026-10-31T12:00:00+00:00",
        "scan_attempts": 1,
        "items": [{
            "symbol": "BTCUSDT",
            "assessment": {"direction": "MIXED"},
            "selection": {"decision": "NO_TRADE"},
            "orders_authorized": False,
        }],
        "orders_authorized": False,
    }
    app = create_app(
        database_path=db, token=TOKEN,
        market_intelligence=lambda: report,
    )
    status, _, body = request(app, path="/api/v1/markets/assessment")
    assert status.startswith("401")
    status, _, body = request(app, path="/api/v1/markets/assessment", auth="Bearer " + TOKEN)
    assert status.startswith("200")
    assert body["items"][0]["symbol"] == "BTCUSDT"
    assert body["items"][0]["orders_authorized"] is False
    assert body["scan_job_status"] == "RUNNING"
    assert body["scan_attempts"] == 1
    assert body["orders_authorized"] is False


def test_market_assessment_endpoint_rejects_unsafe_provider_payload(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass
    app = create_app(
        database_path=db, token=TOKEN,
        market_intelligence=lambda: {
            "status": "HEALTHY",
            "items": [{"symbol": "BTCUSDT", "orders_authorized": True}],
            "orders_authorized": False,
        },
    )
    status, _, body = request(app, path="/api/v1/markets/assessment", auth="Bearer " + TOKEN)
    assert status.startswith("503")
    assert body["items"] == []
    assert body["orders_authorized"] is False


def test_fills_endpoint_returns_read_only_ledger_fills(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass
    app = create_app(database_path=db, token=TOKEN)

    status, _, body = request(app, path="/api/v1/fills", auth="Bearer " + TOKEN)

    assert status.startswith("200")
    assert body["items"] == []
    assert body["orders_authorized"] is False


def test_fills_endpoint_rejects_non_get_methods(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass
    app = create_app(database_path=db, token=TOKEN)

    status, _, body = request(app, path="/api/v1/fills", method="POST", auth="Bearer " + TOKEN)

    assert status.startswith("405")
    assert body["orders_authorized"] is False



def test_api_entrypoint_delegates_to_supervisor_when_enabled(monkeypatch):
    from app.engines import read_only_api
    from backend import shared_market_supervisor

    calls = []
    monkeypatch.setenv("PAPER_MARKET_SUPERVISOR_ENABLED", "true")
    monkeypatch.setattr(shared_market_supervisor, "main", lambda: calls.append("supervisor"))

    read_only_api.main()

    assert calls == ["supervisor"]
