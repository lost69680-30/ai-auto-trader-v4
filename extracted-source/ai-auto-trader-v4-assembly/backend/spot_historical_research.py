"""Bounded public historical Spot candle retrieval for offline research only."""
from __future__ import annotations
from datetime import datetime, timezone
from backend.market_data_adapter import BinanceSpotMarketDataAdapter
from backend.spot_backtesting import SpotBacktestConfig, run_spot_backtest
from backend.strategy_research import Candle

SUPPORTED_INTERVALS = {"1m":1,"3m":3,"5m":5,"15m":15,"30m":30,"1h":60,"2h":120,"4h":240,"6h":360,"8h":480,"12h":720,"1d":1440,"3d":4320,"1w":10080}

class SpotHistoricalResearchService:
    """Public Spot OHLCV to fixed, offline baseline; never places orders."""
    def __init__(self, source=None, config: SpotBacktestConfig | None = None):
        self.source = source or BinanceSpotMarketDataAdapter()
        self.config = config or SpotBacktestConfig()

    def report(self, symbol: str, *, interval: str = "5m", limit: int = 500, now: datetime | None = None) -> dict:
        if not isinstance(symbol, str) or not symbol or symbol != symbol.strip().upper():
            raise ValueError("symbol must be non-empty, trimmed uppercase text")
        if interval not in SUPPORTED_INTERVALS:
            raise ValueError("unsupported Spot interval")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 30 <= limit <= 1500:
            raise ValueError("limit must be an integer from 30 through 1500")
        clock = now or datetime.now(timezone.utc)
        if clock.tzinfo is None or clock.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        rows = self.source.fetch_klines(symbol, interval=interval, limit=limit, now=clock)
        closed = [r for r in rows if r.is_closed]
        candles = [Candle(symbol, r.open_time, r.open, r.high, r.low, r.close, r.volume) for r in closed]
        cfg = SpotBacktestConfig(initial_equity=self.config.initial_equity,
            position_fraction=self.config.position_fraction, fee_bps_per_side=self.config.fee_bps_per_side,
            slippage_bps_per_side=self.config.slippage_bps_per_side,
            candle_interval_minutes=SUPPORTED_INTERVALS[interval], min_candles=self.config.min_candles,
            moving_average_period=self.config.moving_average_period)
        result = run_spot_backtest(symbol, candles, config=cfg, research_now=clock)
        rejected = any(w.startswith("Input rejected:") for w in result.warnings)
        return {"status": "REJECTED" if rejected else ("COMPLETED" if result.trade_count or len(closed) >= cfg.min_candles else "INSUFFICIENT_HISTORY"),
            "market_type": "SPOT", "symbol": symbol, "interval": interval,
            "requested_candles": limit, "closed_candles": len(closed),
            "first_candle_at": closed[0].open_time.isoformat() if closed else None,
            "last_candle_at": closed[-1].open_time.isoformat() if closed else None,
            "ending_equity": result.ending_equity, "net_return_pct": result.net_return_pct,
            "trade_count": result.trade_count, "max_drawdown_pct": result.max_drawdown_pct,
            "total_estimated_costs": result.total_costs, "warnings": list(result.warnings),
            "method": "fixed moving-average baseline; long-only; next-candle-open simulation",
            "orders_authorized": False, "positions_created": False}
