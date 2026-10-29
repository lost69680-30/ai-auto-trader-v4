import asyncio
import json
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

import uvicorn
import websockets

from backend.asgi_runtime import create_read_only_asgi_app


TOKEN = "test-live-stream-token-with-at-least-thirty-two-characters"


def _wsgi_ok(environ, start_response):
    body = b'{"status":"ok","orders_authorized":false}'
    start_response("200 OK", [("Content-Type", "application/json"), ("Content-Length", str(len(body)))])
    return [body]


def test_real_loopback_websocket_delivers_authenticated_read_only_snapshot():
    snapshot = {
        "symbol": "BTCUSDT",
        "market_type": "FUTURES",
        "mark_price": 65000.0,
        "mark_observed_at": datetime.now(timezone.utc).isoformat(),
        "index_price": 64990.0,
        "funding_rate": 0.0001,
        "next_funding_at": (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat(),
        "status": "HEALTHY",
        "orders_authorized": False,
    }
    app = create_read_only_asgi_app(
        http_wsgi_app=_wsgi_ok,
        snapshot_provider=lambda: [snapshot],
        token=TOKEN,
        allowed_symbols={"BTCUSDT"},
        max_age_seconds=90,
        poll_interval_seconds=0.25,
    )

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]

    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="off", access_log=False))
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(sockets=[listener])),
        name="test-asgi-websocket-server",
        daemon=True,
    )
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "ASGI server did not start on loopback"

        async def receive_snapshot():
            async with websockets.connect(
                f"ws://127.0.0.1:{port}/ws/v1/markets",
                additional_headers={"Authorization": f"Bearer {TOKEN}"},
                open_timeout=3,
                close_timeout=1,
            ) as connection:
                return json.loads(await asyncio.wait_for(connection.recv(), timeout=3))

        event = asyncio.run(receive_snapshot())
        assert event["type"] == "market.snapshot"
        assert event["symbol"] == "BTCUSDT"
        assert event["orders_authorized"] is False
        assert event["market_type"] == "FUTURES"
        assert event["mark_price"] == 65000.0
        assert event["index_price"] == 64990.0
        assert event["funding_rate"] == 0.0001
        assert event["next_funding_at"] == snapshot["next_funding_at"]
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
