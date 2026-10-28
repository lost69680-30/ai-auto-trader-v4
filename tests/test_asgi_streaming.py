import asyncio
import json
from datetime import datetime, timezone

from backend.asgi_streaming import create_streaming_asgi_app


TOKEN = "test-stream-token-with-at-least-thirty-two-characters"


def run_asgi(scope, incoming, app):
    sent = []
    queue = list(incoming)

    async def receive():
        if queue:
            return queue.pop(0)
        await asyncio.sleep(0)
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message):
        sent.append(message)
        if message.get("type") == "websocket.send":
            queue.append({"type": "websocket.disconnect", "code": 1000})

    asyncio.run(app(scope, receive, send))
    return sent


def websocket_scope(token=None):
    headers = [(b"host", b"test")]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    return {"type": "websocket", "path": "/ws/v1/markets", "headers": headers}


def test_websocket_rejects_missing_or_invalid_bearer_token():
    app = create_streaming_asgi_app(
        http_app=lambda scope, receive, send: None,
        snapshot_provider=lambda: [],
        token=TOKEN,
        allowed_symbols={"BTCUSDT"},
    )
    sent = run_asgi(websocket_scope(), [{"type": "websocket.connect"}], app)
    assert sent[0]["type"] == "websocket.close"
    assert sent[0]["code"] == 4401


def test_websocket_emits_only_validated_read_only_snapshots():
    snapshot = {
        "symbol": "BTCUSDT",
        "mark_price": 65000.0,
        "mark_observed_at": datetime.now(timezone.utc).isoformat(),
        "status": "HEALTHY",
        "orders_authorized": False,
    }
    app = create_streaming_asgi_app(
        http_app=lambda scope, receive, send: None,
        snapshot_provider=lambda: [snapshot],
        token=TOKEN,
        allowed_symbols={"BTCUSDT"},
        max_age_seconds=90,
        poll_interval_seconds=0.25,
    )
    sent = run_asgi(
        websocket_scope(TOKEN),
        [{"type": "websocket.connect"}],
        app,
    )
    accepted = [message for message in sent if message["type"] == "websocket.accept"]
    payloads = [json.loads(message["text"]) for message in sent if message["type"] == "websocket.send"]
    assert accepted
    assert payloads
    assert payloads[0]["type"] == "market.snapshot"
    assert payloads[0]["symbol"] == "BTCUSDT"
    assert payloads[0]["orders_authorized"] is False


def test_websocket_does_not_emit_stale_or_unhealthy_snapshots():
    snapshot = {
        "symbol": "BTCUSDT",
        "mark_price": 65000.0,
        "mark_observed_at": "2020-01-01T00:00:00+00:00",
        "status": "HEALTHY",
        "orders_authorized": False,
    }
    app = create_streaming_asgi_app(
        http_app=lambda scope, receive, send: None,
        snapshot_provider=lambda: [snapshot],
        token=TOKEN,
        max_age_seconds=90,
        poll_interval_seconds=0.25,
    )
    sent = run_asgi(
        websocket_scope(TOKEN),
        [{"type": "websocket.connect"}],
        app,
    )
    payloads = [message for message in sent if message["type"] == "websocket.send"]
    assert payloads == []

def test_websocket_never_emits_symbols_outside_configured_allowlist():
    snapshot = {
        "symbol": "ETHUSDT",
        "mark_price": 3000.0,
        "mark_observed_at": datetime.now(timezone.utc).isoformat(),
        "status": "HEALTHY",
        "orders_authorized": False,
    }
    app = create_streaming_asgi_app(
        http_app=lambda scope, receive, send: None,
        snapshot_provider=lambda: [snapshot],
        token=TOKEN,
        allowed_symbols={"BTCUSDT"},
        poll_interval_seconds=0.25,
    )
    sent = run_asgi(websocket_scope(TOKEN), [{"type": "websocket.connect"}], app)
    assert not [message for message in sent if message["type"] == "websocket.send"]
