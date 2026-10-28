import json
import io
from datetime import datetime

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
