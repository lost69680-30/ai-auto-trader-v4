"""Opt-in ASGI adapter for the existing authenticated read-only API.

WebSocket transport remains disabled unless PAPER_WEBSOCKET_ENABLED is explicitly
set to a recognized true value. This keeps the existing WSGI runtime as default.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any


def websocket_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Parse the WebSocket feature flag strictly; malformed config fails closed."""
    source = os.environ if environ is None else environ
    raw = source.get("PAPER_WEBSOCKET_ENABLED", "false")
    if not isinstance(raw, str):
        raise ValueError("PAPER_WEBSOCKET_ENABLED must be true/false, yes/no, on/off, or 1/0")
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError("PAPER_WEBSOCKET_ENABLED must be true/false, yes/no, on/off, or 1/0")


def create_read_only_asgi_app(
    *,
    http_wsgi_app: Callable,
    snapshot_provider: Callable[[], list[dict[str, Any]]],
    token: str,
    allowed_symbols: set[str] | frozenset[str],
    max_age_seconds: float = 90.0,
    poll_interval_seconds: float = 5.0,
) -> Callable:
    """Wrap the current WSGI REST app and expose only validated read-only snapshots."""
    from asgiref.wsgi import WsgiToAsgi

    from backend.asgi_streaming import create_streaming_asgi_app

    return create_streaming_asgi_app(
        http_app=WsgiToAsgi(http_wsgi_app),
        snapshot_provider=snapshot_provider,
        token=token,
        allowed_symbols=allowed_symbols,
        max_age_seconds=max_age_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
