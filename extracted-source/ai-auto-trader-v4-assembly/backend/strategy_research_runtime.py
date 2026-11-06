"""Disabled-by-default, read-only walk-forward research over archived Testnet candles.

The runtime evaluates fixed strategy settings, preserves canonical dataset lineage,
and appends results to private PostgreSQL research memory. It has no opportunity
handoff, order, position, risk-setting, or strategy-promotion interface.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from typing import Callable, Iterable

from backend.backtesting import BacktestConfig
from backend.dataset_lineage import DatasetProvenance, candle_dataset_sha256
from backend.market_data_lineage import MarketDataIdentity, build_market_data_manifest
from backend.strategy_research import Candle
from backend.strategy_research_memory import (
    PostgresStrategyResearchMemory,
    StrategyResearchExperiment,
)
from backend.walk_forward import WalkForwardConfig, evaluate_walk_forward


logger = logging.getLogger(__name__)
_RESEARCH_VERSION = "solo-fixed-consensus-walk-forward-v1"
_SUPPORTED_MARKET = ("FUTURES", "BINANCE_USDM_TESTNET")


def _iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("research timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _window_summary(window, *, used_for_selection: bool) -> dict:
    strategy = window.strategy
    return {
        "name": window.name,
        "start_index": window.start_index,
        "end_index": window.end_index,
        "net_pnl": float(strategy.net_pnl),
        "trade_count": int(strategy.trade_count),
        "win_rate_pct": float(strategy.win_rate_pct),
        "max_drawdown_pct": float(strategy.max_drawdown_pct),
        "total_costs": float(strategy.total_costs),
        "buy_hold_return_pct": float(window.buy_hold_return_pct),
        "used_for_selection": used_for_selection,
        "orders_authorized": False,
    }


def _canonical_digest(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_strategy_research_handler(
    archive,
    memory: PostgresStrategyResearchMemory,
    *,
    symbols: Iterable[str],
    candles_per_symbol: int = 3000,
    venue: str = "BINANCE_USDM_TESTNET",
    market_type: str = "FUTURES",
    provider: str = "binance-public-testnet",
    walk_forward_config: WalkForwardConfig | None = None,
    backtest_config: BacktestConfig | None = None,
    clock: Callable[[], datetime] | None = None,
):
    """Create an allowlisted scheduled handler; only archived closed candles are read."""
    configured_symbols = tuple(dict.fromkeys(symbols))
    if not configured_symbols or any(
        not isinstance(symbol, str) or not symbol or symbol != symbol.strip().upper()
        or not symbol.isalnum() for symbol in configured_symbols
    ):
        raise ValueError("research symbols must be a non-empty canonical uppercase allowlist")
    if (market_type, venue) != _SUPPORTED_MARKET:
        raise ValueError("scheduled strategy research is restricted to Futures Testnet archive data")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("dataset provider identifier is required")
    if (
        isinstance(candles_per_symbol, bool)
        or not isinstance(candles_per_symbol, int)
        or not 3000 <= candles_per_symbol <= 100000
    ):
        raise ValueError("candles_per_symbol must be between 3000 and 100000")
    wf_config = walk_forward_config or WalkForwardConfig()
    bt_config = backtest_config or BacktestConfig(candle_interval_minutes=1)
    if wf_config.candle_interval_minutes != 1 or bt_config.candle_interval_minutes != 1:
        raise ValueError("canonical research archive and backtest must use one-minute candles")
    if candles_per_symbol < (
        wf_config.train_bars + wf_config.validation_bars + wf_config.holdout_bars
    ):
        raise ValueError("research history must cover train, validation, and untouched holdout windows")

    def handle(payload: dict, idempotency_key: str) -> dict:
        if not isinstance(payload, dict) or payload.get("scope") != "strategy_research":
            raise ValueError("strategy research job requires scope=strategy_research")
        if payload.get("market_type") != market_type or payload.get("venue") != venue:
            raise ValueError("strategy research job venue does not match its isolated archive")
        if payload.get("orders_authorized", False) is not False:
            raise ValueError("strategy research payload cannot authorize orders")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("strategy research job idempotency key is required")

        now = (clock or (lambda: datetime.now(timezone.utc)))()
        _iso(now)
        evaluated: list[str] = []
        skipped: list[dict[str, str]] = []
        for symbol in configured_symbols:
            try:
                latest = archive.latest_open_time(
                    venue=venue, market_type=market_type, symbol=symbol,
                )
                if latest is None:
                    raise ValueError("no archived candles are available")
                latest_utc = datetime.fromisoformat(_iso(latest))
                start = latest_utc - timedelta(minutes=candles_per_symbol - 1)
                end = latest_utc + timedelta(minutes=1)
                rows = tuple(archive.read_closed_candles(
                    venue=venue, market_type=market_type, symbol=symbol,
                    start=start, end=end, limit=candles_per_symbol,
                ))
                if len(rows) != candles_per_symbol:
                    raise ValueError(
                        f"insufficient archived history: expected {candles_per_symbol}, got {len(rows)}"
                    )
                if any(
                    row.venue != venue or row.market_type != market_type
                    or row.symbol != symbol or row.interval != "1m" or row.is_closed is not True
                    for row in rows
                ):
                    raise ValueError("archive returned a mixed, open, or non-canonical candle series")
                candles = tuple(Candle(
                    symbol=symbol, timestamp=row.open_time,
                    open=row.open, high=row.high, low=row.low,
                    close=row.close, volume=row.volume,
                ) for row in rows)
                canonical_manifest = build_market_data_manifest(
                    candles,
                    MarketDataIdentity(
                        venue=venue, market_type=market_type, symbol=symbol,
                        interval="1m", source_id=provider,
                    ),
                )
                result = evaluate_walk_forward(
                    symbol, candles,
                    config=wf_config,
                    backtest_config=bt_config,
                    research_now=now,
                    dataset_provider=provider,
                    dataset_venue=venue,
                )
                if result.orders_authorized is not False or result.dataset_provenance is None:
                    raise ValueError("walk-forward evaluator returned an unsafe or unproven result")
                provenance = result.dataset_provenance
                if (
                    provenance.provider != provider
                    or provenance.venue != venue
                    or provenance.symbol != symbol
                    or provenance.timeframe != "1m"
                    or provenance.window_start != canonical_manifest.start_timestamp
                    or provenance.window_end != canonical_manifest.end_timestamp
                    or provenance.candle_count != canonical_manifest.row_count
                    or provenance.data_sha256 != candle_dataset_sha256(candles)
                ):
                    raise ValueError("walk-forward provenance does not match the canonical market-data manifest")
                canonical_manifest_dict = canonical_manifest.to_dict()
                manifest = {
                    "provider": provider,
                    "source_identifier": provider,
                    "venue": venue,
                    "market_type": market_type,
                    "symbol": symbol,
                    "interval": "1m",
                    "timeframe": provenance.timeframe,
                    "first_candle_open_time": _iso(provenance.window_start),
                    "last_candle_open_time": _iso(provenance.window_end),
                    "row_count": provenance.candle_count,
                    "sha256_ordered_ohlcv": provenance.data_sha256,
                }
                source_provenance_id = _canonical_digest(canonical_manifest_dict)
                fingerprint_material = {
                    "research_version": _RESEARCH_VERSION,
                    "symbol": symbol,
                    "dataset_manifest": manifest,
                    "market_data_manifest": canonical_manifest_dict,
                    "walk_forward": {
                        "train_bars": wf_config.train_bars,
                        "validation_bars": wf_config.validation_bars,
                        "holdout_bars": wf_config.holdout_bars,
                        "step_bars": wf_config.step_bars,
                        "candle_interval_minutes": wf_config.candle_interval_minutes,
                    },
                    "backtest": {
                        "position_fraction": bt_config.position_fraction,
                        "fee_bps_per_side": bt_config.fee_bps_per_side,
                        "slippage_bps_per_side": bt_config.slippage_bps_per_side,
                        "funding_bps_per_8h": bt_config.funding_bps_per_8h,
                        "minimum_signal_score": bt_config.minimum_signal_score,
                    },
                }
                fingerprint = _canonical_digest(fingerprint_material)
                folds = tuple(_window_summary(fold, used_for_selection=False) for fold in result.folds)
                holdout = _window_summary(result.holdout, used_for_selection=False)
                assumptions = {
                    "research_version": _RESEARCH_VERSION,
                    "dataset_manifest": manifest,
                    "market_data_manifest": canonical_manifest_dict,
                    "walk_forward": fingerprint_material["walk_forward"],
                    "backtest": fingerprint_material["backtest"],
                    "selection_policy": "fixed settings; no parameter tuning; holdout excluded from selection",
                    "orders_authorized": False,
                }
                outcomes = {
                    "folds": list(folds),
                    "holdout": holdout,
                    "warnings": list(result.warnings),
                    "orders_authorized": False,
                }
                experiment = StrategyResearchExperiment(
                    experiment_id=f"solo-exp-{fingerprint[:32]}",
                    candidate_id=f"solo-fixed-consensus:{symbol}",
                    candidate_fingerprint=fingerprint,
                    source_provenance_id=source_provenance_id,
                    experiment_version=1,
                    dataset_start=provenance.window_start,
                    dataset_end=provenance.window_end,
                    configuration_version=_RESEARCH_VERSION,
                    assumptions=assumptions,
                    outcomes=outcomes,
                    status="EVALUATED",
                    failure_reasons=(),
                    orders_authorized=False,
                )
            except (ValueError, TypeError) as exc:
                skipped.append({"symbol": symbol, "reason": f"{type(exc).__name__}: {str(exc)}"[:180]})
                logger.warning(
                    "strategy research skipped symbol=%s reason=%s orders_authorized=False",
                    symbol, type(exc).__name__,
                )
                continue

            # Persistence failures must fail the scheduled job so the durable
            # scheduler retries it; never turn a database outage into a partial success.
            memory.append(
                experiment,
                idempotency_key=f"solo-research:{fingerprint}",
            )
            evaluated.append(symbol)

        if not evaluated:
            raise RuntimeError("strategy research completed; no symbols were evaluated")
        summary = {
            "evaluated": len(evaluated),
            "symbols": evaluated,
            "skipped": skipped,
            "orders_authorized": False,
            "job_idempotency_key": idempotency_key,
        }
        logger.info(
            "strategy research cycle completed evaluated=%d skipped=%d venue=%s orders_authorized=False",
            len(evaluated), len(skipped), venue,
        )
        return summary

    return handle
