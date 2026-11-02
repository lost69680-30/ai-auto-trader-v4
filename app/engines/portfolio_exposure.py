"""Read-only exposure and concentration diagnostics for the paper portfolio.

This module describes marked paper positions. It does not set limits, size
positions, veto orders, or authorize execution; Risk Guardian remains the
exclusive risk authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Iterable

from app.engines.paper_ledger import PaperPosition


@dataclass(frozen=True, slots=True)
class AssetExposure:
    symbol: str
    gross_notional: float
    gross_share_pct: float
    equity_share_pct: float


@dataclass(frozen=True, slots=True)
class PortfolioExposureReport:
    status: str
    position_count: int
    asset_count: int
    gross_exposure: float
    net_exposure: float
    gross_exposure_pct_equity: float
    net_exposure_pct_equity: float
    top_asset_share_pct: float | None
    concentration_hhi: float | None
    exposure_by_symbol: tuple[AssetExposure, ...]
    max_mark_age_seconds: float
    orders_authorized: bool = False


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite numeric data")
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be finite numeric data") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite numeric data")
    return result


def calculate_portfolio_exposure(
    positions: Iterable[PaperPosition],
    *,
    equity: float,
    now: datetime,
    max_mark_age_seconds: float = 30.0,
) -> PortfolioExposureReport:
    """Aggregate fresh OPEN paper positions by asset without making risk decisions."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    stamp = now.astimezone(timezone.utc).timestamp()
    equity_value = _finite(equity, "equity")
    if equity_value <= 0:
        raise ValueError("equity must be positive")
    max_age = _finite(max_mark_age_seconds, "max_mark_age_seconds")
    if max_age <= 0:
        raise ValueError("max_mark_age_seconds must be positive")

    rows = tuple(positions)
    by_symbol: dict[str, float] = {}
    gross = 0.0
    net = 0.0
    open_count = 0
    ages: list[float] = []
    for position in rows:
        if not isinstance(position, PaperPosition):
            raise ValueError("positions must contain PaperPosition records")
        if position.status != "OPEN":
            continue
        symbol = position.symbol
        if (
            not isinstance(symbol, str)
            or not symbol
            or symbol != symbol.strip().upper()
            or not symbol.isalnum()
        ):
            raise ValueError("open position symbol is invalid")
        if position.side not in {"LONG", "SHORT"}:
            raise ValueError("open position side must be LONG or SHORT")
        quantity = _finite(position.quantity, "position quantity")
        mark = _finite(position.mark_price, "position mark price")
        observed = _finite(position.mark_observed_at, "position mark timestamp")
        if quantity <= 0 or mark <= 0 or observed < 0:
            raise ValueError("open position quantity, mark and timestamp must be positive")
        age = stamp - observed
        if age < -2 or age > max_age:
            raise ValueError(f"position mark is stale or future-dated for {symbol}")
        notional = quantity * mark
        if not math.isfinite(notional) or notional <= 0:
            raise ValueError("position notional is invalid")
        gross += notional
        net += notional if position.side == "LONG" else -notional
        by_symbol[symbol] = by_symbol.get(symbol, 0.0) + notional
        open_count += 1
        ages.append(max(0.0, age))

    if not math.isfinite(gross) or not math.isfinite(net) or any(
        not math.isfinite(value) for value in by_symbol.values()
    ):
        raise ValueError("aggregated portfolio exposure is not finite")
    if not open_count:
        return PortfolioExposureReport(
            status="EMPTY",
            position_count=0,
            asset_count=0,
            gross_exposure=0.0,
            net_exposure=0.0,
            gross_exposure_pct_equity=0.0,
            net_exposure_pct_equity=0.0,
            top_asset_share_pct=None,
            concentration_hhi=None,
            exposure_by_symbol=(),
            max_mark_age_seconds=0.0,
            orders_authorized=False,
        )

    assets = tuple(
        AssetExposure(
            symbol=symbol,
            gross_notional=round(notional, 8),
            gross_share_pct=round(notional / gross * 100.0, 6),
            equity_share_pct=round(notional / equity_value * 100.0, 6),
        )
        for symbol, notional in sorted(by_symbol.items())
    )
    shares = [item.gross_notional / gross for item in assets]
    hhi = math.fsum(share * share for share in shares)
    gross_pct_equity = gross / equity_value * 100.0
    net_pct_equity = net / equity_value * 100.0
    top_share_pct = max(shares) * 100.0
    if not all(math.isfinite(value) for value in (hhi, gross_pct_equity, net_pct_equity, top_share_pct)):
        raise ValueError("derived portfolio exposure metrics are not finite")
    return PortfolioExposureReport(
        status="COMPLETE",
        position_count=open_count,
        asset_count=len(assets),
        gross_exposure=round(gross, 8),
        net_exposure=round(net, 8),
        gross_exposure_pct_equity=round(gross_pct_equity, 6),
        net_exposure_pct_equity=round(net_pct_equity, 6),
        top_asset_share_pct=round(top_share_pct, 6),
        concentration_hhi=round(hhi, 8),
        exposure_by_symbol=assets,
        max_mark_age_seconds=round(max(ages), 4),
        orders_authorized=False,
    )
