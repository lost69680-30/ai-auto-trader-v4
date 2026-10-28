"""Coordinate one market poll with the fail-closed paper processing gate.

The service is an integration seam, not a strategy or exchange adapter. A caller
must build a complete orchestration request from each frame; the supplied gate's
processor is responsible for validation, watcher confirmation, risk evaluation,
and paper-ledger recording. This service never authorizes exchange orders.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable, Generic, TypeVar

from app.engines.market_paper_gate import MarketPaperGate
from backend.market_worker import MarketFrame, MarketPollResult

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class PaperCycleItem(Generic[T]):
    symbol: str
    status: str
    reason: str
    result: T | None = None
    orders_authorized: bool = False


@dataclass(frozen=True, slots=True)
class PaperCycleReport(Generic[T]):
    checked_at: float
    items: tuple[PaperCycleItem[T], ...]
    orders_authorized: bool = False

    @property
    def processed_symbols(self) -> tuple[str, ...]:
        return tuple(item.symbol for item in self.items if item.result is not None)

    @property
    def blocked_symbols(self) -> tuple[str, ...]:
        return tuple(item.symbol for item in self.items if item.result is None)


class PaperTradingService(Generic[T]):
    """Route each unique symbol from a worker poll through the paper safety gate."""

    def __init__(
        self,
        *,
        gate: MarketPaperGate[T],
        request_factory: Callable[[MarketFrame], object],
    ) -> None:
        self._gate = gate
        self._request_factory = request_factory

    def process_poll(self, poll: MarketPollResult, *, now: float) -> PaperCycleReport[T]:
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or now < 0:
            raise ValueError("now must be finite and non-negative")

        items: list[PaperCycleItem[T]] = []
        seen_symbols: set[str] = set()
        for frame in poll.frames:
            symbol = frame.symbol
            if symbol in seen_symbols:
                items.append(PaperCycleItem(
                    symbol, "DUPLICATE_SYMBOL_REJECTED",
                    "Poll contains the same symbol more than once; duplicate frame was not processed.",
                ))
                continue
            seen_symbols.add(symbol)

            if poll.orders_authorized or frame.orders_authorized:
                items.append(PaperCycleItem(
                    symbol, "AUTHORIZATION_FLAG_REJECTED",
                    "Market poll/frame unexpectedly claims order authorization; fail closed.",
                ))
                continue

            try:
                request = self._request_factory(frame)
            except Exception as exc:
                items.append(PaperCycleItem(
                    symbol, "REQUEST_BUILD_FAILED",
                    f"Request construction failed ({type(exc).__name__}); frame was not processed.",
                ))
                continue

            try:
                gated = self._gate.process(frame, request, now=float(now))
            except Exception as exc:
                items.append(PaperCycleItem(
                    symbol, "PAPER_PROCESSING_FAILED",
                    f"Paper processing failed ({type(exc).__name__}); cycle continued fail-closed.",
                ))
                continue
            items.append(PaperCycleItem(
                symbol=symbol,
                status=gated.status,
                reason=gated.reason,
                result=gated.result,
                orders_authorized=False,
            ))

        return PaperCycleReport(float(now), tuple(items), orders_authorized=False)
