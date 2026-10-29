"""ASGI wrapper for authenticated, read-only market WebSocket streaming.

The HTTP application is delegated unchanged. WebSocket messages contain validated
market observations only and always explicitly deny order authorization.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

from backend.streaming_contract import authenticate_stream_token, make_market_snapshot_event

logger = logging.getLogger(__name__)
MAX_STREAM_SNAPSHOTS_PER_TICK = 100


def _bearer_token(scope: Mapping[str, Any]) -> str:
    headers = scope.get("headers", ())
    authorization_values = [value for key, value in headers if key.lower() == b"authorization"]
    # Duplicate auth headers can be normalized differently by proxies; reject
    # ambiguity rather than letting intermediaries disagree about credentials.
    if len(authorization_values) != 1:
        return ""
    try:
        raw = authorization_values[0].decode("ascii")
    except (AttributeError, UnicodeDecodeError):
        return ""
    scheme, separator, token = raw.partition(" ")
    if separator and scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return ""


def create_streaming_asgi_app(
    *,
    http_app: Callable,
    snapshot_provider: Callable[[], list[dict]],
    token: str,
    allowed_symbols: set[str] | frozenset[str],
    max_age_seconds: float = 90.0,
    poll_interval_seconds: float = 5.0,
) -> Callable:
    """Route HTTP to the existing app and expose one authenticated market stream.

    WebSocket clients must send Authorization: Bearer <PAPER_API_TOKEN>.
    Missing/weak server tokens, bad credentials, stale data and malformed
    snapshots fail closed. There are no command, order, or control messages.
    """
    if not callable(http_app) or not callable(snapshot_provider):
        raise ValueError("HTTP app and snapshot provider must be callable")
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError("stream token must be at least 32 characters")
    if (not isinstance(allowed_symbols, (set, frozenset)) or not allowed_symbols
            or any(not isinstance(symbol, str) or not symbol or symbol != symbol.upper()
                   or not symbol.isalnum() for symbol in allowed_symbols)):
        raise ValueError("a non-empty uppercase alphanumeric symbol allowlist is required")
    if max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be positive")
    if poll_interval_seconds < 0.25 or poll_interval_seconds > 60:
        raise ValueError("poll_interval_seconds must be between 0.25 and 60 seconds")

    async def app(scope, receive, send):
        if scope.get("type") != "websocket":
            await http_app(scope, receive, send)
            return

        if scope.get("path") != "/ws/v1/markets":
            await send({"type": "websocket.close", "code": 4404, "reason": "not found"})
            return

        # Consume the initial connect event before deciding whether to accept.
        first = await receive()
        if first.get("type") != "websocket.connect":
            await send({"type": "websocket.close", "code": 4400, "reason": "invalid handshake"})
            return
        if not authenticate_stream_token(token, _bearer_token(scope)):
            await send({"type": "websocket.close", "code": 4401, "reason": "unauthorized"})
            return

        await send({"type": "websocket.accept"})
        try:
            while True:
                try:
                    snapshots = await asyncio.to_thread(snapshot_provider)
                except Exception:
                    logger.warning("WebSocket market snapshot provider failed; no event emitted")
                    snapshots = []

                if isinstance(snapshots, list):
                    for snapshot in snapshots[:MAX_STREAM_SNAPSHOTS_PER_TICK]:
                        try:
                            if not isinstance(snapshot, Mapping) or snapshot.get("symbol") not in allowed_symbols:
                                continue
                            event = make_market_snapshot_event(
                                snapshot, max_age_seconds=max_age_seconds
                            )
                        except (TypeError, ValueError):
                            continue
                        await send({
                            "type": "websocket.send",
                            "text": json.dumps(event, separators=(",", ":"), allow_nan=False),
                        })

                try:
                    message = await asyncio.wait_for(receive(), timeout=poll_interval_seconds)
                except asyncio.TimeoutError:
                    continue
                if message.get("type") == "websocket.disconnect":
                    return
                # Client messages are intentionally ignored; this is a one-way feed.
        except Exception:
            logger.warning("WebSocket stream ended unexpectedly; orders remain unauthorized")
            return

    return app
