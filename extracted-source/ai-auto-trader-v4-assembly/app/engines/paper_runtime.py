"""Runnable, paper-only runtime boundary for market polls and persistent simulation.

The runtime accepts an injected analysis handler because deployment-specific
scanner metadata, ATR costs, and position sizing must be explicit. It has no
exchange-order API and never treats unhealthy market frames as execution prices.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Callable, Generic, TypeVar
import threading

from backend.market_data_adapter import MarketCandle
from backend.market_worker import FeedStatus, MarketDataWorker, MarketPollResult
from app.engines.paper_ledger import SQLitePaperLedger
from app.engines.protection import PaperProtectionEngine

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class PaperRuntimeReport(Generic[T]):
    checked_at: float
    analysis_status: str
    analysis_result: T | None
    healthy_symbols: tuple[str, ...]
    unhealthy_symbols: tuple[str, ...]
    fill_statuses: tuple[tuple[str, str], ...]
    protection_status: str
    open_positions: int
    orders_authorized: bool = False


class PaperRuntime(Generic[T]):
    """Run analysis on healthy frames, fill older paper intents, and protect positions."""

    def __init__(
        self,
        *,
        ledger: SQLitePaperLedger,
        protection: PaperProtectionEngine,
        analysis_handler: Callable[[MarketPollResult], T],
        fee_rate: float = 0.0004,
        slippage_bps: float = 2.0,
        max_market_age_seconds: float = 90.0,
    ) -> None:
        for name, value in (("fee_rate", fee_rate), ("slippage_bps", slippage_bps),
                            ("max_market_age_seconds", max_market_age_seconds)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if fee_rate < 0 or slippage_bps < 0 or max_market_age_seconds <= 0:
            raise ValueError("fees and slippage must be non-negative and max market age positive")
        if protection._ledger is not ledger:
            raise ValueError("protection engine must use the same ledger")
        self._ledger = ledger
        self._protection = protection
        self._analysis_handler = analysis_handler
        self._fee_rate = float(fee_rate)
        self._slippage_bps = float(slippage_bps)
        self._max_market_age_seconds = float(max_market_age_seconds)

    def run_forever(
        self,
        worker: MarketDataWorker,
        stop_event: threading.Event,
        *,
        on_report: Callable[[PaperRuntimeReport[T]], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Run the worker continuously until stopped; callback failures do not authorize orders."""
        clock = clock or (lambda: datetime.now().astimezone())

        def consume(poll: MarketPollResult) -> None:
            report = self.process_poll(poll, now=clock().timestamp())
            if on_report is not None:
                on_report(report)

        worker.run_forever(stop_event, on_result=consume, clock=clock)

    @staticmethod
    def _is_finite_number(value: object) -> bool:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        try:
            return math.isfinite(float(value))
        except (OverflowError, TypeError, ValueError):
            return False

    @staticmethod
    def _aware_timestamp(value: object) -> float | None:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            return None
        try:
            stamp = value.timestamp()
        except (OverflowError, OSError, ValueError):
            return None
        return stamp if math.isfinite(stamp) else None

    def _valid_closed_candles(self, symbol: str, candles: object, *, now: float, observed_at: float) -> bool:
        if not isinstance(candles, tuple) or not candles:
            return False
        last_open = None
        last_close = None
        for candle in candles:
            if not isinstance(candle, MarketCandle) or candle.symbol != symbol or candle.is_closed is not True:
                return False
            opened = self._aware_timestamp(candle.open_time)
            closed = self._aware_timestamp(candle.close_time)
            if opened is None or closed is None or opened >= closed or closed > observed_at or closed > now:
                return False
            values = (candle.open, candle.high, candle.low, candle.close, candle.volume, candle.quote_volume)
            if any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not self._is_finite_number(value)
                for value in values
            ):
                return False
            if min(candle.open, candle.high, candle.low, candle.close) <= 0 or candle.volume < 0 or candle.quote_volume < 0:
                return False
            if candle.high < max(candle.open, candle.close, candle.low) or candle.low > min(candle.open, candle.close, candle.high):
                return False
            if isinstance(candle.trades, bool) or not isinstance(candle.trades, int) or candle.trades < 0:
                return False
            if last_open is not None:
                if opened <= last_open or opened <= last_close:
                    return False
                # Runtime is a second boundary after the worker: reject large
                # gaps even if a caller bypasses the normal poller. Permit up
                # to one second for legacy/test candle-close conventions;
                # real exchange kline boundaries normally differ by 1 ms.
                if opened - last_close > 1.0:
                    return False
            last_open, last_close = opened, closed
        return last_close is not None and 0 <= now - last_close <= self._max_market_age_seconds

    def process_poll(self, poll: MarketPollResult, *, now: float) -> PaperRuntimeReport[T]:
        if not self._is_finite_number(now) or now < 0:
            raise ValueError("now must be finite and non-negative")
        if not isinstance(poll, MarketPollResult):
            raise ValueError("poll must be a MarketPollResult")
        checked_at = self._aware_timestamp(poll.checked_at)
        if checked_at is None:
            raise ValueError("poll timestamp must be timezone-aware and finite")
        if checked_at > now:
            raise ValueError("poll timestamp cannot be in the future")
        now = float(now)

        # The worker promises one frame per configured symbol from the same poll.
        # Duplicate symbols create ambiguous marks, so all copies of that symbol
        # are blocked instead of allowing dict overwrite/last-frame-wins behavior.
        symbol_counts: dict[str, int] = {}
        for frame in poll.frames:
            raw_symbol = getattr(frame, "symbol", None)
            if isinstance(raw_symbol, str) and raw_symbol.strip():
                key = raw_symbol.strip().upper()
                symbol_counts[key] = symbol_counts.get(key, 0) + 1
        duplicate_symbols = {symbol for symbol, count in symbol_counts.items() if count > 1}
        poll_age = now - checked_at
        poll_is_fresh = 0 <= poll_age <= self._max_market_age_seconds

        healthy = []
        unhealthy = []
        for frame in poll.frames:
            symbol = getattr(frame, "symbol", "")
            normalized = symbol.strip().upper() if isinstance(symbol, str) else ""
            observed = self._aware_timestamp(getattr(frame, "mark_observed_at", None))
            frame_checked = self._aware_timestamp(getattr(frame, "checked_at", None))
            mark_price = getattr(frame, "mark_price", None)
            valid_price = self._is_finite_number(mark_price) and mark_price > 0
            valid = (
                isinstance(symbol, str) and bool(normalized) and symbol == normalized
                and normalized not in duplicate_symbols
                and frame.status is FeedStatus.HEALTHY
                and poll.orders_authorized is False
                and getattr(frame, "orders_authorized", True) is False
                and poll_is_fresh
                and frame_checked is not None and frame_checked == checked_at
                and 0 <= now - frame_checked <= self._max_market_age_seconds
                and valid_price
                and observed is not None
                and 0 <= now - observed <= self._max_market_age_seconds
                and observed <= now
                and observed is not None
                and self._valid_closed_candles(
                    normalized, getattr(frame, "closed_candles", None), now=now, observed_at=observed,
                )
            )
            (healthy if valid else unhealthy).append(frame)

        # Snapshot pending orders before analysis so newly-created intents cannot
        # receive an instantaneous same-poll fill. They must survive until a later poll.
        pending_before_analysis = {
            order.proposal_id: order for order in self._ledger.orders
            if order.status == "PENDING_PAPER_SIMULATION" and order.recorded_at < now
        }
        analysis_result: T | None = None
        analysis_status = "SKIPPED_UNHEALTHY_MARKET_DATA"
        if healthy and poll.orders_authorized is False:
            # Expose only validated frames to the downstream analysis callback.
            filtered_poll = MarketPollResult(poll.checked_at, tuple(healthy), orders_authorized=False)
            try:
                analysis_result = self._analysis_handler(filtered_poll)
                analysis_status = "ANALYZED"
            except Exception as exc:
                analysis_status = f"ANALYSIS_FAILED:{type(exc).__name__}"
        elif poll.orders_authorized is not False:
            analysis_status = "AUTHORIZATION_FLAG_REJECTED"
            unhealthy.extend(healthy)
            healthy = []

        frames_by_symbol = {frame.symbol: frame for frame in healthy}
        prices = {f.symbol: float(f.mark_price) for f in healthy}
        observed_by_symbol = {f.symbol: self._aware_timestamp(f.mark_observed_at) for f in healthy}
        fills: list[tuple[str, str]] = []
        if not self._ledger.emergency_stop_active:
            for proposal_id, order in pending_before_analysis.items():
                if order.symbol not in prices:
                    continue
                observed_at = observed_by_symbol[order.symbol]
                # Never simulate a fill using a quote that predates the intent.
                if observed_at is None or observed_at < order.recorded_at:
                    continue
                fill = self._ledger.simulate_fill(
                    proposal_id, market_price=prices[order.symbol], filled_at=now,
                    fee_rate=self._fee_rate, slippage_bps=self._slippage_bps,
                )
                fills.append((proposal_id, fill.status))

        protection_results = []
        for symbol, frame in frames_by_symbol.items():
            observed_at = observed_by_symbol[symbol]
            if observed_at is None:
                continue
            try:
                # Preserve source observation time; do not restamp an older quote as now.
                protected = self._protection.process_market_update(
                    {symbol: prices[symbol]}, observed_at=observed_at,
                )
                protection_results.append(protected.status)
            except (ValueError, TypeError):
                protection_results.append("PROTECTION_REJECTED")

        if not protection_results:
            protection_status = "SKIPPED_UNHEALTHY_MARKET_DATA"
        elif "PROTECTION_REJECTED" in protection_results:
            protection_status = "PROTECTION_REJECTED"
        elif "PROTECTION_EXIT" in protection_results:
            protection_status = "PROTECTION_EXIT"
        elif "EMERGENCY_STOP_ACTIVE" in protection_results:
            protection_status = "EMERGENCY_STOP_ACTIVE"
        else:
            protection_status = "MARKET_UPDATED"

        return PaperRuntimeReport(
            checked_at=now, analysis_status=analysis_status,
            analysis_result=analysis_result,
            healthy_symbols=tuple(frame.symbol for frame in healthy),
            unhealthy_symbols=tuple(frame.symbol for frame in unhealthy),
            fill_statuses=tuple(fills), protection_status=protection_status,
            open_positions=len(self._ledger.open_positions), orders_authorized=False,
        )
