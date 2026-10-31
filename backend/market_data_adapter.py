"""Public, read-only Binance USDⓈ-M Futures market-data adapter.

Only public GET endpoints are used. This module intentionally has no API-key,
account, leverage, or order-placement interface.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from backend.scanner import Contract, MarketSnapshot

PRODUCTION_BASE_URL = "https://fapi.binance.com"
TESTNET_BASE_URL = "https://demo-fapi.binance.com"
SUPPORTED_INTERVALS = frozenset({
    "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h",
    "12h", "1d", "3d", "1w", "1M",
})


class MarketDataError(RuntimeError):
    """Public market data could not be retrieved or was structurally invalid."""


@dataclass(frozen=True, slots=True)
class MarketCandle:
    symbol: str
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    trades: int
    is_closed: bool


@dataclass(frozen=True, slots=True)
class MarketUniverse:
    contracts: tuple[Contract, ...]
    snapshots: tuple[MarketSnapshot, ...]
    fetched_at: datetime
    orders_authorized: bool = False


@dataclass(frozen=True, slots=True)
class FundingRateRecord:
    symbol: str
    funding_rate: float
    funding_time: datetime
    mark_price: float | None = None
    orders_authorized: bool = False


@dataclass(frozen=True, slots=True)
class MarkPriceSnapshot:
    symbol: str
    mark_price: float
    index_price: float | None
    last_funding_rate: float | None
    next_funding_at: datetime | None
    observed_at: datetime
    orders_authorized: bool = False
    market_type: str = "FUTURES"


Transport = Callable[[str, Mapping[str, object], float], object]


def _number(value: object, field: str) -> float:
    try:
        result = float(value)  # also rejects unparseable upstream fields
    except (TypeError, ValueError, OverflowError) as exc:
        raise MarketDataError(f"invalid numeric field: {field}") from exc
    if not math.isfinite(result):
        raise MarketDataError(f"non-finite numeric field: {field}")
    return result


def _epoch_ms(value: object, field: str) -> datetime:
    try:
        number = int(value)
        return datetime.fromtimestamp(number / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise MarketDataError(f"invalid timestamp field: {field}") from exc


def _aware_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(timezone.utc)


_FIXED_KLINE_INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
}


def _validate_kline_interval_boundaries(
    open_time: datetime, close_time: datetime, interval: str
) -> None:
    """Reject candles whose UTC timestamps do not match their exchange interval."""
    if interval == "1M":
        if (
            open_time.day != 1
            or open_time.hour != 0
            or open_time.minute != 0
            or open_time.second != 0
            or open_time.microsecond != 0
        ):
            raise MarketDataError("kline open time is not aligned to interval boundary")
        next_month = (
            datetime(open_time.year + 1, 1, 1, tzinfo=timezone.utc)
            if open_time.month == 12
            else datetime(open_time.year, open_time.month + 1, 1, tzinfo=timezone.utc)
        )
        expected_close = next_month - timedelta(milliseconds=1)
    else:
        interval_ms = _FIXED_KLINE_INTERVAL_MS.get(interval)
        if interval_ms is None:
            raise MarketDataError("unsupported kline interval boundary")
        if interval == "1w":
            aligned = (
                open_time.weekday() == 0
                and open_time.hour == 0
                and open_time.minute == 0
                and open_time.second == 0
                and open_time.microsecond == 0
            )
        else:
            open_ms = int(open_time.timestamp() * 1000)
            aligned = open_ms % interval_ms == 0
        if not aligned:
            raise MarketDataError("kline open time is not aligned to interval boundary")
        expected_close = open_time + timedelta(milliseconds=interval_ms - 1)

    if close_time != expected_close:
        raise MarketDataError("kline close time does not match interval boundary")


class BinanceUsdMFuturesAdapter:
    """Fetch public market metadata, 24h statistics, top-of-book, and OHLCV candles.

    ``transport`` is an injectable seam for deterministic tests. It receives the
    REST path, query parameters, and timeout; production defaults to urllib.
    ``testnet=True`` targets Binance Futures Testnet's public REST host.
    """

    def __init__(
        self,
        *,
        testnet: bool = False,
        base_url: str | None = None,
        timeout_seconds: float = 5.0,
        transport: Transport | None = None,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if base_url is not None and not base_url.startswith("https://"):
            raise ValueError("base_url must use HTTPS")
        self.base_url = (base_url or (TESTNET_BASE_URL if testnet else PRODUCTION_BASE_URL)).rstrip("/")
        self.kline_path = "/fapi/v1/klines"
        self.timeout_seconds = timeout_seconds
        self._transport = transport or self._urllib_get

    def _urllib_get(self, path: str, params: Mapping[str, object], timeout: float) -> object:
        url = f"{self.base_url}{path}?{urlencode(params)}"
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "ai-auto-trader-v4-readonly/1.0"})
        try:
            with urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                if status < 200 or status >= 300:
                    raise MarketDataError(f"Binance returned HTTP {status} for {path}")
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise MarketDataError(f"Binance returned HTTP {exc.code} for {path}") from exc
        except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MarketDataError(f"unable to retrieve public market data from {path}") from exc

    def _get(self, path: str, params: Mapping[str, object] | None = None) -> object:
        try:
            return self._transport(path, params or {}, self.timeout_seconds)
        except MarketDataError:
            raise
        except Exception as exc:
            raise MarketDataError(f"public market-data request failed for {path}") from exc

    @staticmethod
    def _list_payload(payload: object, path: str) -> list[object]:
        if not isinstance(payload, list):
            raise MarketDataError(f"unexpected response shape from {path}")
        return payload

    @staticmethod
    def _unique_symbol_rows(rows: list[object], label: str) -> dict[str, Mapping[str, object]]:
        result: dict[str, Mapping[str, object]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                raise MarketDataError(f"malformed {label} row")
            symbol = row.get("symbol")
            if not isinstance(symbol, str) or not symbol or symbol != symbol.upper():
                raise MarketDataError(f"invalid symbol in {label}")
            if symbol in result:
                raise MarketDataError(f"duplicate {label} symbol: {symbol}")
            result[symbol] = row
        return result

    def fetch_funding_rate_history(
        self, symbol: str, *, start_time_ms: int | None = None,
        end_time_ms: int | None = None, limit: int = 1000,
    ) -> tuple[FundingRateRecord, ...]:
        """Fetch public funding-rate history for schedule/rate cross-checks only.

        This endpoint does not expose account-specific funding cash movements;
        it cannot by itself reconcile the user's actual realized funding payments.
        """
        if not isinstance(symbol, str) or not symbol or symbol != symbol.upper():
            raise ValueError("symbol must be non-empty uppercase text")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        params: dict[str, object] = {"symbol": symbol, "limit": limit}
        for name, value in (("startTime", start_time_ms), ("endTime", end_time_ms)):
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"{name} must be a non-negative integer timestamp")
                params[name] = value
        if start_time_ms is not None and end_time_ms is not None and start_time_ms > end_time_ms:
            raise ValueError("start_time_ms cannot exceed end_time_ms")
        payload = self._list_payload(self._get("/fapi/v1/fundingRate", params), "/fapi/v1/fundingRate")
        records: list[FundingRateRecord] = []
        seen: set[tuple[str, int]] = set()
        for item in payload:
            if not isinstance(item, Mapping) or item.get("symbol") != symbol:
                raise MarketDataError("malformed or mismatched funding history row")
            rate = _number(item.get("fundingRate"), "fundingRate")
            try:
                stamp = int(item.get("fundingTime"))
                moment = _epoch_ms(stamp, "fundingTime")
            except (TypeError, ValueError, OverflowError) as exc:
                raise MarketDataError("invalid fundingTime") from exc
            raw_mark = item.get("markPrice")
            mark = _number(raw_mark, "markPrice") if raw_mark not in (None, "") else None
            if mark is not None and mark <= 0:
                raise MarketDataError("funding history mark price must be positive")
            key = (symbol, stamp)
            if key in seen:
                raise MarketDataError("duplicate funding history event")
            seen.add(key)
            records.append(FundingRateRecord(symbol, rate, moment, mark, False))
        records.sort(key=lambda r: r.funding_time)
        return tuple(records)

    def fetch_market_universe(self, *, now: datetime | None = None) -> MarketUniverse:
        """Join exchangeInfo, 24h ticker, and book ticker into scanner-ready records.

        ``volatility_pct`` is a simple trailing 24h high-low range as a percentage
        of last price, not a forecast or annualized statistical volatility.
        ``observed_at`` is the UTC fetch timestamp for the joined local snapshot.
        Invalid/missing per-symbol quote data is omitted. Endpoint failures abort
        the entire snapshot rather than silently returning partial universe data.
        """
        supplied_clock = now is not None
        requested_at = _aware_utc(now)
        exchange_payload = self._get("/fapi/v1/exchangeInfo")
        if not isinstance(exchange_payload, Mapping) or not isinstance(exchange_payload.get("symbols"), list):
            raise MarketDataError("unexpected response shape from /fapi/v1/exchangeInfo")

        contracts: list[Contract] = []
        seen_contracts: set[str] = set()
        for row in exchange_payload["symbols"]:
            if not isinstance(row, Mapping):
                raise MarketDataError("malformed exchangeInfo symbol row")
            symbol, status, contract_type, quote_asset = (
                row.get("symbol"), row.get("status"), row.get("contractType"), row.get("quoteAsset")
            )
            if not all(isinstance(value, str) and value for value in (symbol, status, contract_type, quote_asset)):
                raise MarketDataError("exchangeInfo row missing required contract fields")
            if symbol != symbol.upper() or symbol in seen_contracts:
                raise MarketDataError(f"invalid or duplicate exchangeInfo symbol: {symbol}")
            seen_contracts.add(symbol)
            tick_size: float | None = None
            quantity_step: float | None = None
            min_quantity: float | None = None
            max_quantity: float | None = None
            min_notional: float | None = None
            filters = row.get("filters")
            if isinstance(filters, list):
                def exactly_one(filter_type: str):
                    matches = [item for item in filters if isinstance(item, Mapping)
                               and item.get("filterType") == filter_type]
                    return matches[0] if len(matches) == 1 else None
                price_filter = exactly_one("PRICE_FILTER")
                # Prefer MARKET_LOT_SIZE for market-like paper fills, but require
                # a valid LOT_SIZE as well when metadata is used for limit sizing.
                lot_filter = exactly_one("LOT_SIZE")
                market_lot_filter = exactly_one("MARKET_LOT_SIZE")
                notional_filter = exactly_one("MIN_NOTIONAL") or exactly_one("NOTIONAL")
                try:
                    parsed_tick = _number(price_filter.get("tickSize"), "tickSize") if price_filter else 0.0
                    selected_lot = market_lot_filter or lot_filter
                    parsed_step = _number(selected_lot.get("stepSize"), "stepSize") if selected_lot else 0.0
                    parsed_min = _number(selected_lot.get("minQty"), "minQty") if selected_lot else 0.0
                    raw_max = selected_lot.get("maxQty") if selected_lot else None
                    parsed_max = _number(raw_max, "maxQty") if raw_max is not None else 0.0
                    parsed_notional = _number(notional_filter.get("notional", notional_filter.get("minNotional")), "minNotional") if notional_filter else 0.0
                    if parsed_tick > 0 and parsed_step > 0 and parsed_min > 0:
                        tick_size, quantity_step, min_quantity = parsed_tick, parsed_step, parsed_min
                        if parsed_max >= parsed_min:
                            max_quantity = parsed_max
                    if parsed_notional > 0:
                        min_notional = parsed_notional
                except (MarketDataError, AttributeError):
                    # Keep unknown metadata absent; consumers must reject sizing.
                    pass
            contracts.append(Contract(
                symbol, status, contract_type, quote_asset,
                tick_size=tick_size, quantity_step=quantity_step, min_quantity=min_quantity,
                max_quantity=max_quantity, min_notional=min_notional,
            ))

        ticker_rows = self._unique_symbol_rows(
            self._list_payload(self._get("/fapi/v1/ticker/24hr"), "/fapi/v1/ticker/24hr"), "24h ticker"
        )
        book_rows = self._unique_symbol_rows(
            self._list_payload(self._get("/fapi/v1/ticker/bookTicker"), "/fapi/v1/ticker/bookTicker"), "book ticker"
        )
        # Use a completion timestamp by default, so request latency does not make
        # a just-received snapshot appear older than it is. Tests can inject a clock.
        fetched_at = requested_at if supplied_clock else datetime.now(timezone.utc)
        snapshots: list[MarketSnapshot] = []
        for symbol in sorted(ticker_rows.keys() & book_rows.keys()):
            ticker, book = ticker_rows[symbol], book_rows[symbol]
            try:
                last = _number(ticker.get("lastPrice"), "lastPrice")
                quote_volume = _number(ticker.get("quoteVolume"), "quoteVolume")
                high = _number(ticker.get("highPrice"), "highPrice")
                low = _number(ticker.get("lowPrice"), "lowPrice")
                bid = _number(book.get("bidPrice"), "bidPrice")
                ask = _number(book.get("askPrice"), "askPrice")
                if last <= 0 or quote_volume < 0 or low <= 0 or high < low or bid <= 0 or ask < bid:
                    continue
                mid = (bid + ask) / 2.0
                if mid <= 0:
                    continue
                spread_bps = (ask - bid) / mid * 10_000.0
                volatility_pct = (high - low) / last * 100.0
                if not all(math.isfinite(value) for value in (spread_bps, volatility_pct)):
                    continue
                snapshots.append(MarketSnapshot(
                    symbol=symbol,
                    observed_at=fetched_at,
                    last_price=last,
                    quote_volume_24h=quote_volume,
                    spread_bps=spread_bps,
                    volatility_pct=volatility_pct,
                ))
            except MarketDataError:
                # Skip only this malformed symbol; never synthesize default prices.
                continue

        return MarketUniverse(tuple(contracts), tuple(snapshots), fetched_at, orders_authorized=False)

    def fetch_server_time(self) -> datetime:
        """Fetch exchange server time from the public, unauthenticated endpoint."""
        payload = self._get("/fapi/v1/time")
        if not isinstance(payload, Mapping):
            raise MarketDataError("unexpected response shape from /fapi/v1/time")
        raw_timestamp = payload.get("serverTime")
        if isinstance(raw_timestamp, bool) or not isinstance(raw_timestamp, (int, str)):
            raise MarketDataError("invalid timestamp field: serverTime")
        try:
            timestamp_ms = int(raw_timestamp)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MarketDataError("invalid timestamp field: serverTime") from exc
        if timestamp_ms <= 0:
            raise MarketDataError("invalid timestamp field: serverTime")
        return _epoch_ms(timestamp_ms, "serverTime")

    def fetch_mark_price(self, symbol: str) -> MarkPriceSnapshot:
        """Fetch the public mark/index price and funding context for one symbol."""
        if not isinstance(symbol, str) or not symbol or symbol != symbol.upper().strip():
            raise ValueError("symbol must be non-empty, trimmed and uppercase-normalized")
        payload = self._get("/fapi/v1/premiumIndex", {"symbol": symbol})
        if not isinstance(payload, Mapping) or payload.get("symbol") != symbol:
            raise MarketDataError("unexpected response shape from /fapi/v1/premiumIndex")
        mark = _number(payload.get("markPrice"), "markPrice")
        index = _number(payload.get("indexPrice"), "indexPrice")
        funding = _number(payload.get("lastFundingRate"), "lastFundingRate")
        if mark <= 0 or index <= 0:
            raise MarketDataError("mark and index prices must be positive")
        observed_at = _epoch_ms(payload.get("time"), "premiumIndex time")
        next_funding = _epoch_ms(payload.get("nextFundingTime"), "nextFundingTime")
        return MarkPriceSnapshot(
            symbol=symbol, mark_price=mark, index_price=index,
            last_funding_rate=funding, next_funding_at=next_funding,
            observed_at=observed_at, orders_authorized=False,
        )

    def fetch_klines(
        self,
        symbol: str,
        *,
        interval: str = "1m",
        limit: int = 100,
        now: datetime | None = None,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> tuple[MarketCandle, ...]:
        """Retrieve validated klines; current in-progress candle is marked open."""

        if not isinstance(symbol, str) or not symbol or symbol != symbol.upper():
            raise ValueError("symbol must be non-empty and uppercase-normalized")
        if interval not in SUPPORTED_INTERVALS:
            raise ValueError("unsupported kline interval")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1500:
            raise ValueError("limit must be an integer from 1 through 1500")
        for name, value in (("start_time_ms", start_time_ms), ("end_time_ms", end_time_ms)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"{name} must be a non-negative integer")
        if start_time_ms is not None and end_time_ms is not None and end_time_ms < start_time_ms:
            raise ValueError("end_time_ms must be greater than or equal to start_time_ms")
        supplied_clock = now is not None
        requested_at = _aware_utc(now)
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        if end_time_ms is not None:
            params["endTime"] = end_time_ms
        payload = self._list_payload(self._get(self.kline_path, params), self.kline_path)
        # Resolve the wall clock at response completion by default, so a request
        # crossing a candle boundary does not misclassify the new candle as future.
        now_utc = requested_at if supplied_clock else datetime.now(timezone.utc)
        parsed: list[MarketCandle] = []
        last_open: datetime | None = None
        for row in payload:
            if not isinstance(row, list) or len(row) < 12:
                raise MarketDataError("malformed kline row")
            open_time = _epoch_ms(row[0], "kline open time")
            close_time = _epoch_ms(row[6], "kline close time")
            _validate_kline_interval_boundaries(open_time, close_time, interval)
            opened = _number(row[1], "kline open")
            high = _number(row[2], "kline high")
            low = _number(row[3], "kline low")
            close = _number(row[4], "kline close")
            volume = _number(row[5], "kline volume")
            quote_volume = _number(row[7], "kline quote volume")
            try:
                trades = int(row[8])
            except (TypeError, ValueError, OverflowError) as exc:
                raise MarketDataError("invalid kline trade count") from exc
            if (min(opened, high, low, close) <= 0 or volume < 0 or quote_volume < 0 or trades < 0
                    or high < max(opened, close, low) or low > min(opened, close, high)
                    or close_time < open_time):
                raise MarketDataError("invalid OHLCV data in kline row")
            if last_open is not None and open_time <= last_open:
                raise MarketDataError("kline timestamps must be strictly increasing")
            if open_time > now_utc:
                raise MarketDataError("kline open time is in the future")
            last_open = open_time
            parsed.append(MarketCandle(
                symbol=symbol, open_time=open_time, close_time=close_time,
                open=opened, high=high, low=low, close=close, volume=volume,
                quote_volume=quote_volume, trades=trades, is_closed=close_time < now_utc,
            ))
        return tuple(parsed)


class BinanceSpotMarketDataAdapter(BinanceUsdMFuturesAdapter):
    """Read-only Binance Spot candles and last-trade price for analysis only.

    This adapter is intentionally separate from the USDⓈ-M Futures adapter.
    The worker's generic ``mark_price`` field carries Spot's ``lastPrice`` here;
    it is not a futures mark price or index price. It has no trading interface.
    """

    SPOT_BASE_URL = "https://data-api.binance.vision"

    def __init__(self, *, base_url: str | None = None, timeout_seconds: float = 5.0,
                 transport: Transport | None = None) -> None:
        super().__init__(base_url=base_url or self.SPOT_BASE_URL,
                         timeout_seconds=timeout_seconds, transport=transport)
        self.kline_path = "/api/v3/klines"

    def fetch_mark_price(self, symbol: str) -> MarkPriceSnapshot:
        """Return Spot last price with local receive time, not Futures mark data.

        Binance's 24-hour ticker ``closeTime`` is the window's last-trade time,
        not a dedicated quote-observation clock. The market worker separately
        validates candle timestamps/freshness; use local receive time here so a
        malformed or skewed 24-hour-window timestamp cannot be mistaken for the
        instant this response was observed.
        """
        if not isinstance(symbol, str) or not symbol or symbol != symbol.upper().strip():
            raise ValueError("symbol must be non-empty, trimmed and uppercase-normalized")
        payload = self._get("/api/v3/ticker/24hr", {"symbol": symbol})
        if not isinstance(payload, Mapping) or payload.get("symbol") != symbol:
            raise MarketDataError("unexpected response shape from /api/v3/ticker/24hr")
        price = _number(payload.get("lastPrice"), "lastPrice")
        if price <= 0:
            raise MarketDataError("spot last price must be positive")
        observed_at = datetime.now(timezone.utc)
        return MarkPriceSnapshot(
            symbol=symbol, mark_price=price, index_price=None,
            last_funding_rate=None, next_funding_at=None,
            observed_at=observed_at, orders_authorized=False, market_type="SPOT",
        )
