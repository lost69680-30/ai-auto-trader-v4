"""Token-authenticated paper API for ledger inspection and emergency-stop activation.

Only the emergency-stop activation endpoint mutates state. No order placement, fill,
stop-clear, or exchange execution mutation endpoints exist. Deploy behind HTTPS.
"""
from __future__ import annotations

import hmac
import logging
import json
import os
import tempfile
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import parse_qs

from app.engines.paper_ledger import SQLitePaperLedger
from backend.timeframe_aggregation import aggregate_complete_yearly_candles
from app.engines.postgres_paper_ledger import PostgresPaperLedger


def _open_ledger(database_path: str):
    """Select PostgreSQL only when explicitly configured; never silently fall back."""
    if "PAPER_DATABASE_URL" in os.environ:
        dsn = os.environ.get("PAPER_DATABASE_URL", "").strip()
        if not dsn:
            raise RuntimeError("PAPER_DATABASE_URL is set but empty; refusing local SQLite fallback.")
        return PostgresPaperLedger(dsn)
    return SQLitePaperLedger(database_path)


def _safe_startup_error(exc: Exception) -> str:
    """Return bounded startup diagnostics with the configured database URI redacted."""
    message = str(exc)
    dsn = os.environ.get("PAPER_DATABASE_URL", "").strip()
    if dsn:
        message = message.replace(dsn, "[database URI redacted]")
    message = " ".join(message.split())[:240]
    return f"{type(exc).__name__}: {message}"

def _json_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _state_storage_status(database_path: str) -> dict[str, object]:
    """Describe the selected paper-ledger storage backend without exposing credentials."""
    if "PAPER_DATABASE_URL" in os.environ:
        if not os.environ.get("PAPER_DATABASE_URL", "").strip():
            return {
                "durability": "UNAVAILABLE",
                "survives_restart": False,
                "warning": "PAPER_DATABASE_URL is configured but empty; ledger operations fail closed.",
            }
        return {
            "durability": "POSTGRESQL",
            "survives_restart": True,
            "warning": (
                "Paper ledger is configured for external PostgreSQL. Recovery depends on the "
                "database remaining available and the migration being applied; orders remain paper-only."
            ),
        }
    warning = (
        "SQLite file storage is not proof of persistence across a host restart or redeploy. "
        "Configure and verify a persistent disk or external durable database before relying "
        "on emergency-stop or paper-ledger state recovery."
    )
    try:
        resolved = Path(database_path).expanduser().resolve()
        temporary_root = Path(tempfile.gettempdir()).resolve()
        if resolved == temporary_root or temporary_root in resolved.parents:
            return {
                "durability": "EPHEMERAL",
                "survives_restart": False,
                "warning": (
                    "The configured SQLite path is under the temporary directory. Paper ledger "
                    "and emergency-stop state may be lost on host restart, redeploy or spin-down. "
                    "Use verified persistent storage before relying on recovery."
                ),
            }
    except (OSError, RuntimeError, ValueError):
        pass
    return {
        "durability": "UNVERIFIED",
        "survives_restart": None,
        "warning": warning,
    }


def _position_dict(p) -> dict[str, object]:
    return {
        "proposal_id": p.proposal_id, "symbol": p.symbol, "side": p.side,
        "quantity": p.quantity, "entry_price": p.entry_price, "mark_price": p.mark_price,
        "stop_loss": p.stop_loss, "take_profit": p.take_profit, "opened_at": p.opened_at,
        "mark_observed_at": p.mark_observed_at, "status": p.status,
        "opening_fee": p.opening_fee, "closing_fee": p.closing_fee,
        "funding_pnl": p.funding_pnl, "unrealized_pnl": p.unrealized_pnl,
        "total_pnl": p.total_pnl, "closed_at": p.closed_at,
    }


def _fetch_candle_intervals(source, symbol: str, intervals: tuple[str, ...], limit: int,
                            max_workers: int = 4):
    """Fetch independent candle intervals concurrently while isolating interval failures."""
    if not intervals:
        return {}, {}, {}
    worker_count = max(1, min(max_workers, len(intervals), 4))
    def fetch_one(requested_interval: str):
        try:
            rows = source.fetch_klines(symbol, interval=requested_interval, limit=limit)
            payload = [{
                "open_time": row.open_time.isoformat(), "close_time": row.close_time.isoformat(),
                "open": row.open, "high": row.high, "low": row.low, "close": row.close,
                "volume": row.volume, "is_closed": row.is_closed,
            } for row in rows if row.is_closed]
            status = "HEALTHY" if payload else "INSUFFICIENT_HISTORY"
            return requested_interval, rows, payload, status
        except Exception as exc:
            return requested_interval, (), [], f"UNAVAILABLE:{type(exc).__name__}"
    rows_by_interval, interval_payloads, interval_status = {}, {}, {}
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="candle-history") as pool:
        for interval, rows, payload, status in pool.map(fetch_one, intervals):
            rows_by_interval[interval] = rows
            interval_payloads[interval] = payload
            interval_status[interval] = status
    return interval_payloads, interval_status, rows_by_interval


def create_app(*, database_path: str, token: str, market_status: Callable[[], dict] | None = None,
               market_items: Callable[[], list[dict]] | None = None,
               market_candles: Callable[[str], dict] | None = None,
               account_status: Callable[[], dict] | None = None,
               spot_research: Callable[[], dict] | None = None):
    """Create WSGI app. A strong API token is mandatory; no permissive defaults."""
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError("API token must be at least 32 characters; configure it as a secret")
    if not database_path or database_path == ":memory:":
        raise ValueError("API requires a durable file-backed paper ledger")
    state_storage = _state_storage_status(database_path)

    def app(environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/")
        supplied = environ.get("HTTP_AUTHORIZATION", "")
        expected = "Bearer " + token
        authorized = hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
        if not authorized:
            body, status = _json_bytes({"error": "unauthorized", "orders_authorized": False}), "401 Unauthorized"
        elif method == "POST" and path == "/api/v1/emergency-stop":
            try:
                raw_length = environ.get("CONTENT_LENGTH", "0")
                if not raw_length.isdigit() or int(raw_length) > 2048:
                    raise ValueError("invalid or oversized request body")
                raw_body = environ["wsgi.input"].read(int(raw_length))
                data = json.loads(raw_body.decode("utf-8")) if raw_body else {}
                reason = data.get("reason") if isinstance(data, dict) else None
                if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 200:
                    raise ValueError("reason must contain 1 to 200 characters")
                with _open_ledger(database_path) as ledger:
                    ledger.activate_emergency_stop(occurred_at=time.time(), reason=reason.strip())
                    payload = {"emergency_stop_active": ledger.emergency_stop_active,
                               "emergency_stop_reason": ledger.emergency_stop_reason,
                               "orders_authorized": False, "mode": "PAPER_ONLY",
                               "state_storage": state_storage}
                body, status = _json_bytes(payload), "200 OK"
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                body, status = _json_bytes({"error": "invalid_request", "orders_authorized": False}), "400 Bad Request"
            except Exception:
                body, status = _json_bytes({"error": "ledger_unavailable", "orders_authorized": False}), "503 Service Unavailable"
        elif method != "GET":
            body, status = _json_bytes({"error": "method_not_allowed", "orders_authorized": False}), "405 Method Not Allowed"
        else:
            if path == "/api/v1/status":
                try:
                    with _open_ledger(database_path) as ledger:
                        feed = market_status() if market_status else {"status": "UNKNOWN", "orders_authorized": False}
                        payload = {
                            "service": "ai-auto-trader-v4", "mode": "PAPER_ONLY",
                            "orders_authorized": False, "emergency_stop_active": ledger.emergency_stop_active,
                            "emergency_stop_reason": ledger.emergency_stop_reason,
                            "open_position_count": len(ledger.open_positions), "feed": feed,
                            "state_storage": state_storage, "server_time": time.time(),
                            "analysis_runtime": {
                                "status": "READ_ONLY_MARKET_DATA_ONLY",
                                "research_pipeline": "NOT_STARTED",
                                "paper_simulation": "NOT_STARTED",
                                "orders_authorized": False,
                                "reason": (
                                    "This deployment runs the validated public market-data poller and "
                                    "read-only API only; the unattended research-to-paper simulation "
                                    "worker is not started."
                                ),
                            },
                        }
                    body, status = _json_bytes(payload), "200 OK"
                except Exception:
                    body, status = _json_bytes({"error": "ledger_unavailable", "orders_authorized": False}), "503 Service Unavailable"
            elif path == "/api/v1/spot/research":
                try:
                    if spot_research is None:
                        body, status = _json_bytes({
                            "status": "NOT_CONFIGURED",
                            "opportunities": [],
                            "rejections": [],
                            "orders_authorized": False,
                        }), "503 Service Unavailable"
                    else:
                        payload = spot_research()
                        if (not isinstance(payload, dict)
                                or payload.get("market_type") != "SPOT"
                                or payload.get("orders_authorized") is not False
                                or not isinstance(payload.get("opportunities"), list)
                                or any(
                                    not isinstance(item, dict)
                                    or item.get("orders_authorized") is not False
                                    for item in payload.get("opportunities", [])
                                )):
                            raise ValueError("Spot research provider returned an invalid or unsafe response")
                        body, status = _json_bytes(payload), "200 OK"
                except Exception:
                    body, status = _json_bytes({
                        "status": "UNAVAILABLE",
                        "opportunities": [],
                        "rejections": [],
                        "orders_authorized": False,
                    }), "503 Service Unavailable"
            elif path == "/api/v1/markets/candles":
                try:
                    query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
                    symbol_values = query.get("symbol", [])
                    symbol = symbol_values[0].strip().upper() if len(symbol_values) == 1 else ""
                    if not symbol or len(symbol) > 30 or not symbol.isalnum() or market_candles is None:
                        body, status = _json_bytes({"error": "invalid_or_unavailable_candle_request", "orders_authorized": False}), "400 Bad Request"
                    else:
                        payload = market_candles(symbol)
                        if not isinstance(payload, dict) or payload.get("symbol") != symbol or payload.get("orders_authorized") is not False:
                            raise ValueError("candle provider returned an invalid or unsafe response")
                        body, status = _json_bytes(payload), "200 OK"
                except ValueError:
                    body, status = _json_bytes({"error": "invalid_or_unavailable_candle_request", "orders_authorized": False}), "400 Bad Request"
                except Exception:
                    body, status = _json_bytes({"error": "market_candles_unavailable", "orders_authorized": False}), "503 Service Unavailable"
            elif path == "/api/v1/binance-spot-testnet/account":
                try:
                    if account_status is None:
                        payload = {"connected": False, "mode": "BINANCE_SPOT_TESTNET_READ_ONLY",
                                   "status": "NOT_CONFIGURED", "balances": [], "orders_authorized": False}
                        body, status = _json_bytes(payload), "503 Service Unavailable"
                    else:
                        payload = account_status()
                        if (not isinstance(payload, dict)
                                or payload.get("orders_authorized") is not False
                                or not isinstance(payload.get("balances"), list)
                                or payload.get("mode") != "BINANCE_SPOT_TESTNET_READ_ONLY"):
                            raise ValueError("account provider returned an invalid or unsafe response")
                        body, status = _json_bytes(payload), ("200 OK" if payload.get("connected") else "503 Service Unavailable")
                except Exception:
                    body, status = _json_bytes({"connected": False, "mode": "BINANCE_SPOT_TESTNET_READ_ONLY",
                        "status": "UNAVAILABLE", "balances": [], "orders_authorized": False}), "503 Service Unavailable"
            elif path == "/api/v1/markets":
                try:
                    feed = market_status() if market_status else {"status": "UNKNOWN", "orders_authorized": False}
                    items = market_items() if market_items else []
                    healthy = feed.get("status") == "HEALTHY" and bool(items)
                    payload = {"status": feed.get("status", "UNKNOWN"), "items": items,
                               "checked_at": feed.get("checked_at"),
                               "reason": feed.get("reason"), "orders_authorized": False}
                    body, status = _json_bytes(payload), ("200 OK" if healthy else "503 Service Unavailable")
                except Exception:
                    body, status = _json_bytes({"status": "UNAVAILABLE", "items": [],
                        "reason": "market feed unavailable", "orders_authorized": False}), "503 Service Unavailable"
            elif path in ("/api/v1/positions", "/api/v1/orders", "/api/v1/audit", "/api/v1/funding"):
                try:
                    with _open_ledger(database_path) as ledger:
                        if path.endswith("/positions"):
                            payload = {"items": [_position_dict(p) for p in ledger.positions],
                                       "orders_authorized": False, "state_storage": state_storage}
                        elif path.endswith("/orders"):
                            payload = {"items": [vars_order(o) for o in ledger.orders],
                                       "orders_authorized": False, "state_storage": state_storage}
                        elif path.endswith("/funding"):
                            payload = {"items": [dict(e) for e in ledger.funding_events],
                                       "orders_authorized": False, "state_storage": state_storage}
                        else:
                            payload = {"items": [{"event_id": e.event_id, "event_type": e.event_type,
                                "proposal_id": e.proposal_id, "occurred_at": e.occurred_at,
                                "payload": dict(e.payload)} for e in ledger.audit_events()],
                                "orders_authorized": False, "state_storage": state_storage}
                    body, status = _json_bytes(payload), "200 OK"
                except Exception:
                    body, status = _json_bytes({"error": "ledger_unavailable"}), "503 Service Unavailable"
            elif path == "/api/v1/risk":
                try:
                    with _open_ledger(database_path) as ledger:
                        snapshots = ledger._connection.execute(
                            "SELECT * FROM paper_equity_snapshots ORDER BY snapshot_at DESC LIMIT 1"
                        ).fetchone()
                        payload = {"emergency_stop_active": ledger.emergency_stop_active,
                                   "open_position_count": len(ledger.open_positions), "orders_authorized": False,
                                   "latest_snapshot": dict(snapshots) if snapshots else None,
                                   "state_storage": state_storage}
                    body, status = _json_bytes(payload), "200 OK"
                except Exception:
                    body, status = _json_bytes({"error": "ledger_unavailable"}), "503 Service Unavailable"
            else:
                body, status = _json_bytes({"error": "not_found"}), "404 Not Found"
        start_response(status, [("Content-Type", "application/json; charset=utf-8"),
                                ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
                                ("X-Content-Type-Options", "nosniff")])
        return [body]
    return app


def vars_order(order) -> dict[str, object]:
    return {"proposal_id": order.proposal_id, "signal_id": order.signal_id, "symbol": order.symbol,
            "side": order.side, "quantity": order.quantity, "entry_price": order.entry_price,
            "stop_loss": order.stop_loss, "take_profit": order.take_profit,
            "recorded_at": order.recorded_at, "status": order.status}


class MarketFeedCache:
    """Thread-safe latest validated public market snapshot; never grants execution authority."""
    def __init__(self, max_age_seconds: float = 90.0) -> None:
        self._lock = threading.Lock()
        self._max_age = max_age_seconds
        self._items: list[dict[str, object]] = []
        self._checked_at: float | None = None
        self._status = "STARTING"
        self._reason: str | None = "waiting for first public market-data poll"

    def publish(self, result) -> None:
        rows: list[dict[str, object]] = []
        unhealthy = []
        market_type = getattr(result, "market_type", "FUTURES")
        for frame in result.frames:
            if frame.status != "HEALTHY" and getattr(frame.status, "value", None) != "HEALTHY":
                unhealthy.append(f"{frame.symbol}:{getattr(frame.status, 'value', frame.status)}")
                continue
            if market_type == "SPOT":
                if frame.last_price is None or frame.last_observed_at is None:
                    unhealthy.append(f"{frame.symbol}:INVALID_DATA")
                    continue
                row = {
                    "symbol": frame.symbol, "market_type": "SPOT",
                    "last_price": frame.last_price,
                    "last_observed_at": frame.last_observed_at.isoformat(),
                    "checked_at": frame.checked_at.isoformat(),
                    "status": "HEALTHY",
                    "candles": [{"open_time": c.open_time.isoformat(), "close_time": c.close_time.isoformat(),
                        "open": c.open, "high": c.high, "low": c.low, "close": c.close,
                        "volume": c.volume, "is_closed": c.is_closed} for c in frame.closed_candles[-100:]],
                    "orders_authorized": False,
                }
                rows.append(row)
                continue
            if frame.mark_price is None:
                unhealthy.append(f"{frame.symbol}:INVALID_DATA")
                continue
            rows.append({
                "symbol": frame.symbol, "market_type": "FUTURES",
                "mark_price": frame.mark_price,
                "mark_observed_at": frame.mark_observed_at.isoformat() if frame.mark_observed_at else None,
                "checked_at": frame.checked_at.isoformat(),
                "status": getattr(frame.status, "value", frame.status),
                "funding_rate": frame.funding_rate,
                "candles": [{"open_time": c.open_time.isoformat(), "close_time": c.close_time.isoformat(),
                    "open": c.open, "high": c.high, "low": c.low, "close": c.close,
                    "volume": c.volume, "is_closed": c.is_closed} for c in frame.closed_candles[-100:]],
                "orders_authorized": False,
            })
        all_healthy = bool(result.frames) and len(rows) == len(result.frames)
        with self._lock:
            self._items = rows
            self._checked_at = result.checked_at.timestamp()
            self._status = "HEALTHY" if all_healthy and rows else ("DEGRADED" if rows else "OFFLINE")
            self._reason = None if all_healthy and rows else ("; ".join(unhealthy) or "no validated market snapshots")

    def fail(self, reason: str) -> None:
        with self._lock:
            self._status, self._reason = "OFFLINE", reason
            self._checked_at = time.time()
            self._items = []

    def status(self) -> dict:
        with self._lock:
            status, reason, checked = self._status, self._reason, self._checked_at
            age = None if checked is None else time.time() - checked
            if age is None or age < 0 or age > self._max_age:
                status, reason = "STALE" if checked is not None else "STARTING", "market snapshot is missing or stale"
            return {"status": status, "reason": reason, "checked_at": checked,
                    "age_seconds": age, "orders_authorized": False}

    def items(self) -> list[dict]:
        with self._lock:
            age = None if self._checked_at is None else time.time() - self._checked_at
            if age is None or age < 0 or age > self._max_age or self._status not in ("HEALTHY", "DEGRADED"):
                return []
            return [dict(item) for item in self._items]


def main() -> None:
    """Run the authenticated read-only API and independent public market poller."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from backend.market_data_adapter import BinanceSpotMarketDataAdapter, BinanceUsdMFuturesAdapter
    from backend.binance_spot_testnet_account import BinanceSpotTestnetAccountClient
    from backend.market_worker import MarketDataWorker, MarketWorkerConfig
    from backend.spot_market_worker import SpotMarketDataWorker, SpotMarketWorkerConfig

    token = os.environ.get("PAPER_API_TOKEN", "")
    database = os.environ.get("PAPER_DATABASE_PATH", "./data/paper-ledger.sqlite3")
    host = os.environ.get("PAPER_API_HOST", "127.0.0.1")
    port = int(os.environ.get("PAPER_API_PORT", "8080"))
    if host not in ("127.0.0.1", "localhost") and os.environ.get("PAPER_API_HTTPS_TERMINATED") != "1":
        raise SystemExit("Refusing non-loopback bind unless HTTPS termination is configured")
    max_age = float(os.environ.get("PAPER_MAX_MARKET_AGE_SECONDS", "90"))
    symbols_raw = os.environ.get("PAPER_MARKET_SYMBOLS", "BTCUSDT,ETHUSDT")
    symbols = tuple(dict.fromkeys(item.strip().upper() for item in symbols_raw.split(",") if item.strip()))
    interval = os.environ.get("PAPER_MARKET_INTERVAL", "1m")
    poll_interval = float(os.environ.get("PAPER_MARKET_POLL_INTERVAL_SECONDS", "15"))
    candle_limit = int(os.environ.get("PAPER_MARKET_CANDLE_LIMIT", "100"))
    testnet = os.environ.get("PAPER_MARKET_TESTNET", "false").strip().lower() in ("1", "true", "yes", "on")
    market_mode = os.environ.get("PAPER_MARKET_DATA_MODE", "futures").strip().lower()
    if market_mode not in ("futures", "spot"):
        raise SystemExit("PAPER_MARKET_DATA_MODE must be futures or spot")
    if market_mode == "spot" and testnet:
        raise SystemExit("Spot market-data mode is public production data; disable PAPER_MARKET_TESTNET")
    if not symbols:
        raise SystemExit("PAPER_MARKET_SYMBOLS must include at least one symbol")

    try:
        with _open_ledger(database) as ledger:
            ledger.verify_integrity()
    except Exception as exc:
        raise SystemExit(
            "Paper ledger startup integrity check failed; refusing to start the API. "
            + _safe_startup_error(exc)
        ) from None
    storage_state = _state_storage_status(database)
    if storage_state["durability"] == "POSTGRESQL":
        logging.info(
            "Paper ledger uses PostgreSQL and startup integrity checks passed. "
            "No exchange orders are authorized."
        )
    elif storage_state["durability"] == "EPHEMERAL":
        logging.warning(
            "Configured paper ledger storage is ephemeral; emergency-stop and ledger state "
            "may not survive a host restart or redeploy. orders_authorized=False"
        )
    else:
        logging.warning(
            "Configured paper ledger storage durability is unverified; do not rely on restart "
            "recovery until persistent storage is confirmed. orders_authorized=False"
        )

    cache = MarketFeedCache(max_age_seconds=max_age)
    binance_testnet_account = BinanceSpotTestnetAccountClient()
    if market_mode == "spot":
        source = BinanceSpotMarketDataAdapter()
        worker = SpotMarketDataWorker(source, SpotMarketWorkerConfig(
            symbols=symbols, interval=interval, candle_limit=candle_limit,
            max_market_age_seconds=max_age, poll_interval_seconds=poll_interval,
        ))
        logging.info("market data mode=SPOT_PUBLIC_ANALYSIS_ONLY; Spot last price is distinct from Futures mark price")
    else:
        source = BinanceUsdMFuturesAdapter(testnet=testnet)
        worker = MarketDataWorker(source, MarketWorkerConfig(
            symbols=symbols, interval=interval, candle_limit=candle_limit,
            poll_interval_seconds=poll_interval, max_market_age_seconds=max_age,
        ))
        logging.info("market data mode=%s", "FUTURES_TESTNET" if testnet else "FUTURES_PRODUCTION_PUBLIC")
    analysis_candle_limit = int(os.environ.get("PAPER_ANALYSIS_CANDLE_LIMIT", "500"))
    if not 50 <= analysis_candle_limit <= 500:
        raise SystemExit("PAPER_ANALYSIS_CANDLE_LIMIT must be between 50 and 500")
    # Binance supplies native weekly ("1w") and monthly ("1M") candles.
    # Yearly candles are derived only from 12 complete, closed monthly bars.
    analysis_intervals = ("1m", "5m", "15m", "1h", "4h", "1d", "1w", "1M")

    def market_candles(symbol: str) -> dict:
        if symbol not in symbols:
            raise ValueError("symbol is not in the configured market allowlist")
        interval_payloads, interval_status, rows_by_interval = _fetch_candle_intervals(
            source, symbol, analysis_intervals, analysis_candle_limit
        )

        yearly = aggregate_complete_yearly_candles(rows_by_interval.get("1M", ()))
        interval_payloads["1y"] = yearly
        if yearly:
            interval_status["1y"] = "HEALTHY"
        elif interval_status.get("1M", "").startswith("UNAVAILABLE:"):
            interval_status["1y"] = "UNAVAILABLE:MONTHLY_INTERVAL"
        else:
            interval_status["1y"] = "INSUFFICIENT_HISTORY"
        return {"symbol": symbol, "candles_by_interval": interval_payloads,
                "interval_status": interval_status, "orders_authorized": False, "mode": "PAPER_ONLY"}

    stop = threading.Event()

    def poll_loop() -> None:
        while not stop.is_set():
            try:
                result = worker.poll_once()
                cache.publish(result)
                logging.info("market feed status=%s healthy=%d unhealthy=%d orders_authorized=False",
                             cache.status()["status"], len(result.healthy_symbols), len(result.unhealthy_symbols))
                for frame in result.frames:
                    if frame.status.value != "HEALTHY":
                        # Reasons are validation diagnostics only; never log credentials or headers.
                        logging.warning("market symbol=%s status=%s reasons=%s retry_count=%d orders_authorized=False",
                                        frame.symbol, frame.status.value,
                                        "; ".join(frame.reasons) or "no detailed reason supplied",
                                        frame.retry_count)
                delay = worker.next_delay_seconds
            except Exception as exc:
                cache.fail(f"market polling failed: {type(exc).__name__}")
                logging.exception("Market polling failed; feed remains fail-closed")
                delay = 15.0
            stop.wait(delay)

    poller = threading.Thread(target=poll_loop, name="readonly-market-poller", daemon=True)
    poller.start()
    try:
        from asgiref.wsgi import WsgiToAsgi
        import uvicorn
        from backend.asgi_streaming import create_streaming_asgi_app

        spot_research_provider = None
        if market_mode == "spot":
            from backend.spot_market_research import SpotMarketResearchService, SpotResearchConfig
            spot_research_service = SpotMarketResearchService(
                source=source,
                config=SpotResearchConfig(
                    symbols=symbols,
                    interval=interval,
                    candle_limit=analysis_candle_limit,
                    max_candle_age_seconds=max_age,
                ),
            )

            def spot_research_provider():
                report = spot_research_service.analyze_once()
                return {
                    "status": report.status,
                    "market_type": "SPOT",
                    "observed_at": report.observed_at.isoformat(),
                    "market_scan": {
                        "candidates": [
                            {"symbol": row.symbol, "score": row.score, "decision": row.decision,
                             "reasons": list(row.reasons)}
                            for row in report.market_scan.candidates
                        ],
                        "rejections": [
                            {"symbol": row.symbol, "reason": row.reason}
                            for row in report.market_scan.rejections
                        ],
                    },
                    "opportunities": [
                        {"symbol": row.symbol, "direction": row.direction, "score": row.score,
                         "rank": row.rank, "patterns": list(row.patterns), "evidence": list(row.evidence),
                         "timestamp": row.timestamp.isoformat(),
                         "estimated_round_trip_cost_pct": row.estimated_round_trip_cost_pct,
                         "status": row.status, "orders_authorized": False}
                        for row in report.opportunities
                    ],
                    "rejections": [
                        {"symbol": row.symbol, "reason": row.reason}
                        for row in report.rejections
                    ],
                    "orders_authorized": False,
                }

        wsgi_api = create_app(database_path=database, token=token,
                              market_status=cache.status, market_items=cache.items,
                              market_candles=market_candles,
                              account_status=binance_testnet_account.account_summary,
                              spot_research=spot_research_provider)
        asgi_api = create_streaming_asgi_app(
            http_app=WsgiToAsgi(wsgi_api),
            snapshot_provider=cache.items,
            token=token,
            allowed_symbols=frozenset(symbols),
            max_age_seconds=max_age,
            poll_interval_seconds=max(1.0, min(poll_interval, 10.0)),
        )
        logging.info("Read-only paper API listening on %s:%s; WebSocket /ws/v1/markets enabled; no execution routes",
                     host, port)
        uvicorn.run(asgi_api, host=host, port=port, log_level="info", lifespan="off")
    finally:
        stop.set()
        poller.join(timeout=5)


if __name__ == "__main__":
    main()
