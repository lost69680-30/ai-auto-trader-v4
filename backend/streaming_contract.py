"""Validated, versioned read-only messages for a future streaming transport.

This module defines the stream contract only. It does not open a socket or alter
the currently deployed REST API. All events are analysis/observation data; they
never authorize exchange execution.
"""
from __future__ import annotations

import hmac
import math
from datetime import datetime, timezone
from typing import Mapping


def authenticate_stream_token(expected_token: str, supplied_token: str) -> bool:
    """Fail closed for missing/weak server tokens and compare tokens in constant time."""
    if not isinstance(expected_token, str) or len(expected_token) < 32:
        return False
    if not isinstance(supplied_token, str) or not supplied_token:
        return False
    return hmac.compare_digest(expected_token.encode("utf-8"), supplied_token.encode("utf-8"))


def _aware_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("mark timestamp is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("mark timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("mark timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def make_market_snapshot_event(
    snapshot: Mapping[str, object],
    *,
    now: datetime | None = None,
    max_age_seconds: float = 90.0,
) -> dict[str, object]:
    """Create a minimal safe event, rejecting stale, malformed, or unsafe snapshots."""
    if not isinstance(snapshot, Mapping):
        raise ValueError("snapshot must be a mapping")
    if not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be finite and positive")
    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise ValueError("now must include a timezone")
    checked_at = checked_at.astimezone(timezone.utc)

    symbol = snapshot.get("symbol")
    if not isinstance(symbol, str) or not symbol or symbol != symbol.upper() or not symbol.isalnum():
        raise ValueError("symbol must be uppercase alphanumeric text")
    status = snapshot.get("status")
    if status != "HEALTHY":
        raise ValueError("only healthy snapshots may be emitted as market.snapshot")
    if snapshot.get("orders_authorized") is not False:
        raise ValueError("stream events must explicitly preserve orders_authorized=False")

    market_type = snapshot.get("market_type", "FUTURES")
    if market_type == "SPOT":
        raw_price = snapshot.get("last_price")
        price_field = "last_price"
        timestamp_field = "last_observed_at"
    elif market_type == "FUTURES":
        raw_price = snapshot.get("mark_price")
        price_field = "mark_price"
        timestamp_field = "mark_observed_at"
    else:
        raise ValueError("market_type must be SPOT or FUTURES")
    if isinstance(raw_price, bool) or not isinstance(raw_price, (int, float)):
        raise ValueError(f"{price_field} must be numeric")
    price = float(raw_price)
    if not math.isfinite(price) or price <= 0:
        raise ValueError(f"{price_field} must be finite and positive")

    observed = _aware_datetime(snapshot.get(timestamp_field))
    age_seconds = (checked_at - observed).total_seconds()
    if age_seconds < -1.0:
        raise ValueError("future-dated market snapshots are rejected")
    if age_seconds > max_age_seconds:
        raise ValueError("stale market snapshots are rejected")

    index_raw = snapshot.get("index_price")
    funding_raw = snapshot.get("funding_rate")
    next_funding_raw = snapshot.get("next_funding_at")
    futures_context = (index_raw, funding_raw, next_funding_raw)
    if market_type == "SPOT":
        if any(value is not None for value in futures_context):
            raise ValueError("Spot snapshots must not include Futures-only context")
        normalized_futures_context = None
    else:
        context_present = tuple(value is not None for value in futures_context)
        if any(context_present) and not all(context_present):
            raise ValueError("Futures index/funding context must be complete")
        if not any(context_present):
            normalized_futures_context = None
        else:
            if (isinstance(index_raw, bool) or not isinstance(index_raw, (int, float))
                    or not math.isfinite(index_raw) or index_raw <= 0):
                raise ValueError("index_price must be finite and positive")
            if (isinstance(funding_raw, bool) or not isinstance(funding_raw, (int, float))
                    or not math.isfinite(funding_raw)):
                raise ValueError("funding_rate must be finite")
            next_funding = _aware_datetime(next_funding_raw)
            if next_funding <= checked_at:
                raise ValueError("next_funding_at must be in the future")
            normalized_futures_context = {
                "index_price": float(index_raw),
                "funding_rate": float(funding_raw),
                "next_funding_at": next_funding.isoformat(),
            }

    event = {
        "schema_version": 1,
        "type": "market.snapshot",
        "symbol": symbol,
        "market_type": market_type,
        "observed_at": observed.isoformat(),
        "status": "HEALTHY",
        "orders_authorized": False,
    }
    if market_type == "SPOT":
        event["last_price"] = price
    else:
        event["mark_price"] = price
        if normalized_futures_context is not None:
            event.update(normalized_futures_context)
    return event
