import io
import json

from app.engines.paper_ledger import SQLitePaperLedger
from app.engines.read_only_api import create_app

TOKEN = "unit-test-token-that-is-at-least-32-characters-long"


def request(app, path, auth="Bearer " + TOKEN):
    captured = {}
    def start(status, headers):
        captured["status"] = status
    route, _, query = path.partition("?")
    environ = {
        "REQUEST_METHOD": "GET", "PATH_INFO": route, "QUERY_STRING": query,
        "HTTP_AUTHORIZATION": auth, "CONTENT_LENGTH": "0", "wsgi.input": io.BytesIO(b""),
    }
    body = b"".join(app(environ, start))
    return captured["status"], json.loads(body)


def test_testnet_account_route_reports_read_only_balances(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass
    account = {
        "connected": True, "mode": "BINANCE_SPOT_TESTNET_READ_ONLY",
        "status": "CONNECTED", "balances": [{"asset": "USDT", "free": "5", "locked": "0"}],
        "orders_authorized": False,
    }
    app = create_app(database_path=db, token=TOKEN, account_status=lambda: account)
    status, body = request(app, "/api/v1/binance-spot-testnet/account")
    assert status.startswith("200")
    assert body["balances"][0]["asset"] == "USDT"
    assert body["orders_authorized"] is False


def test_testnet_account_route_fails_closed_when_not_configured(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    with SQLitePaperLedger(db):
        pass
    app = create_app(database_path=db, token=TOKEN)
    status, body = request(app, "/api/v1/binance-spot-testnet/account")
    assert status.startswith("503")
    assert body["connected"] is False
    assert body["balances"] == []
    assert body["orders_authorized"] is False
