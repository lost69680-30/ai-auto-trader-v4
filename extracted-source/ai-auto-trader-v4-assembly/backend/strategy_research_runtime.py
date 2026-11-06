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
import math
from typing import Callable, Iterable

from backend.backtesting import BacktestConfig
from backend.cost_sensitivity import summarize_validation_cost_sensitivity
from backend.dataset_lineage import DatasetProvenance, candle_dataset_sha256
from backend.external_strategy_intelligence import ExternalRuleSpec, assess_walk_forward_quality
from backend.market_data_lineage import MarketDataIdentity, build_market_data_manifest
from backend.market_data_adapter import MarketDataError
from backend.strategy_research import Candle
from backend.strategy_research_memory import (
    PostgresStrategyResearchMemory,
    StrategyResearchExperiment,
)
from backend.walk_forward import WalkForwardConfig, evaluate_walk_forward


logger = logging.getLogger(__name__)
_RESEARCH_VERSION = "solo-four-family-candidate-benchmark-v2-cost-sensitivity"
_RESEARCH_CANDIDATES = (
    ("trend_sma_cross", ExternalRuleSpec("SMA_CROSS", {"fast_period": 10, "slow_period": 30}, "ONE_BAR")),
    ("momentum_20", ExternalRuleSpec("MOMENTUM", {"lookback": 20, "threshold_pct": 0.15}, "ONE_BAR")),
    ("breakout_20", ExternalRuleSpec("BREAKOUT", {"lookback": 20, "buffer_pct": 0.05}, "ONE_BAR")),
    ("mean_reversion_rsi", ExternalRuleSpec("RSI_THRESHOLD", {"period": 14, "oversold": 30.0, "overbought": 70.0}, "ONE_BAR")),
)
_SUPPORTED_MARKET = ("FUTURES", "BINANCE_USDM_TESTNET")


def _iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("research timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _window_summary(window, *, used_for_selection: bool) -> dict:
    strategy = window.strategy
    initial_equity = float(strategy.initial_equity)
    if not math.isfinite(initial_equity) or initial_equity <= 0:
        raise ValueError("research window initial equity must be finite and positive")
    regime = window.market_regime
    return {
        "name": window.name,
        "start_index": window.start_index,
        "end_index": window.end_index,
        "net_pnl": float(strategy.net_pnl),
        "strategy_net_return_pct": float(strategy.net_pnl) / initial_equity * 100.0,
        "trade_count": int(strategy.trade_count),
        "win_rate_pct": float(strategy.win_rate_pct),
        "max_drawdown_pct": float(strategy.max_drawdown_pct),
        "total_costs": float(strategy.total_costs),
        "buy_hold_return_pct": float(window.buy_hold_return_pct),
        "market_regime": {
            "trend_state": regime.trend_state,
            "volatility_state": regime.volatility_state,
            "trend_return_pct": float(regime.trend_return_pct),
            "realized_volatility_pct": float(regime.realized_volatility_pct),
            "observations": int(regime.observations),
            "methodology": regime.methodology,
            "orders_authorized": False,
        },
        "used_for_selection": used_for_selection,
        "orders_authorized": False,
    }


def _canonical_digest(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()



def fetch_stateless_closed_candles(
    source,
    *,
    symbol: str,
    count: int,
    now: datetime,
    page_size: int = 1500,
) -> tuple[Candle, ...]:
    """Fetch a bounded, contiguous 1m research window directly from the Testnet source."""
    if not callable(getattr(source, "fetch_klines", None)):
        raise ValueError("stateless research source must provide fetch_klines")
    if not isinstance(symbol, str) or not symbol or symbol != symbol.strip().upper():
        raise ValueError("stateless research symbol must be canonical uppercase text")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("stateless research candle count must be positive")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 1500:
        raise ValueError("stateless research page_size must be between 1 and 1500")
    _iso(now)
    now = now.astimezone(timezone.utc)
    end_time_ms = int(now.timestamp() * 1000) - 1
    by_open_ms: dict[int, object] = {}
    max_pages = math.ceil(count / page_size) + 2
    for _ in range(max_pages):
        remaining = count - len(by_open_ms)
        if remaining <= 0:
            break
        page = source.fetch_klines(
            symbol, interval="1m", limit=min(page_size, remaining),
            end_time_ms=end_time_ms, now=now,
        )
        if not isinstance(page, (tuple, list)):
            raise ValueError("stateless market source returned an invalid candle page")
        if not page:
            break
        page_open_ms: list[int] = []
        for row in page:
            if getattr(row, "symbol", None) != symbol:
                raise ValueError("stateless market source returned a mismatched symbol")
            if getattr(row, "is_closed", False) is not True:
                continue
            open_time = getattr(row, "open_time", None)
            close_time = getattr(row, "close_time", None)
            if (
                not isinstance(open_time, datetime) or open_time.tzinfo is None or open_time.utcoffset() is None
                or not isinstance(close_time, datetime) or close_time.tzinfo is None or close_time.utcoffset() is None
            ):
                raise ValueError("stateless market source returned an invalid candle timestamp")
            open_time = open_time.astimezone(timezone.utc)
            close_time = close_time.astimezone(timezone.utc)
            if open_time.second or open_time.microsecond or close_time >= now or close_time < open_time:
                raise ValueError("stateless market source returned an open, future or malformed candle")
            open_ms = int(open_time.timestamp() * 1000)
            if open_ms in by_open_ms:
                raise ValueError("stateless market source returned duplicate candles")
            by_open_ms[open_ms] = row
            page_open_ms.append(open_ms)
        if not page_open_ms:
            break
        end_time_ms = min(page_open_ms) - 1
    ordered_rows = [by_open_ms[key] for key in sorted(by_open_ms)]
    if len(ordered_rows) < count:
        raise ValueError(
            f"insufficient stateless history: expected {count} closed candles, got {len(ordered_rows)}"
        )
    ordered_rows = ordered_rows[-count:]
    for previous, current in zip(ordered_rows, ordered_rows[1:]):
        if current.open_time.astimezone(timezone.utc) - previous.open_time.astimezone(timezone.utc) != timedelta(minutes=1):
            raise ValueError("stateless research candle history is not continuous")
    return tuple(Candle(
        symbol=symbol, timestamp=row.open_time.astimezone(timezone.utc),
        open=row.open, high=row.high, low=row.low, close=row.close, volume=row.volume,
    ) for row in ordered_rows)


def build_strategy_research_handler(
    archive,
    memory: PostgresStrategyResearchMemory,
    *,
    source=None,
    symbols: Iterable[str],
    candles_per_symbol: int = 3000,
    venue: str = "BINANCE_USDM_TESTNET",
    market_type: str = "FUTURES",
    provider: str = "binance-public-testnet",
    walk_forward_config: WalkForwardConfig | None = None,
    backtest_config: BacktestConfig | None = None,
    clock: Callable[[], datetime] | None = None,
):
    """Create an allowlisted handler over archived or bounded on-demand closed Testnet candles."""
    configured_symbols = tuple(dict.fromkeys(symbols))
    if not configured_symbols or any(
        not isinstance(symbol, str) or not symbol or symbol != symbol.strip().upper()
        or not symbol.isalnum() for symbol in configured_symbols
    ):
        raise ValueError("research symbols must be a non-empty canonical uppercase allowlist")
    if archive is None and not callable(getattr(source, "fetch_klines", None)):
        raise ValueError("stateless strategy research requires a read-only market source")
    if (market_type, venue) != _SUPPORTED_MARKET:
        raise ValueError("scheduled strategy research is restricted to Futures Testnet market data")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("dataset provider identifier is required")
    if (
        isinstance(candles_per_symbol, bool)
        or not isinstance(candles_per_symbol, int)
        or not 3000 <= candles_per_symbol <= 10000
    ):
        raise ValueError("candles_per_symbol must be between 3000 and 10000")
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
            raise ValueError("strategy research job venue does not match its isolated Testnet source")
        if payload.get("orders_authorized", False) is not False:
            raise ValueError("strategy research payload cannot authorize orders")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("strategy research job idempotency key is required")

        now = (clock or (lambda: datetime.now(timezone.utc)))()
        _iso(now)
        evaluated: list[str] = []
        evaluated_symbols: set[str] = set()
        skipped: list[dict[str, str]] = []
        for symbol in configured_symbols:
            try:
                if archive is None:
                    candles = fetch_stateless_closed_candles(
                        source, symbol=symbol, count=candles_per_symbol, now=now,
                    )
                else:
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
                manifest = canonical_manifest.to_dict()
                source_provenance_id = _canonical_digest(manifest)
                canonical_digest = candle_dataset_sha256(candles)
                dataset_manifest = {
                    "provider": provider,
                    "source_identifier": provider,
                    "venue": venue,
                    "market_type": market_type,
                    "symbol": symbol,
                    "interval": "1m",
                    "timeframe": "1m",
                    "first_candle_open_time": _iso(canonical_manifest.start_timestamp),
                    "last_candle_open_time": _iso(canonical_manifest.end_timestamp),
                    "row_count": canonical_manifest.row_count,
                    "sha256_ordered_ohlcv": canonical_digest,
                }
            except (ValueError, TypeError, MarketDataError) as exc:
                skipped.append({"symbol": symbol, "reason": f"{type(exc).__name__}: {str(exc)}"[:180]})
                logger.warning(
                    "strategy research skipped symbol=%s reason=%s detail=%s orders_authorized=False",
                    symbol, type(exc).__name__, " ".join(str(exc).split())[:160],
                )
                continue

            for candidate_id, rule_spec in _RESEARCH_CANDIDATES:
                try:
                    result = evaluate_walk_forward(
                        symbol, candles,
                        config=wf_config,
                        backtest_config=bt_config,
                        research_now=now,
                        dataset_provider=provider,
                        dataset_venue=venue,
                        rule_spec=rule_spec,
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
                        or provenance.data_sha256 != canonical_digest
                    ):
                        raise ValueError("walk-forward provenance does not match the canonical market-data manifest")

                    quality = assess_walk_forward_quality(result)
                    cost_sensitivity = summarize_validation_cost_sensitivity(result)
                    cost_sensitivity_summary = {
                        "status": cost_sensitivity.status,
                        "reason": cost_sensitivity.reason,
                        "methodology": "recompute chronological validation trades at 1.0x, 1.5x and 2.0x recorded estimated costs; no untouched holdout data is read",
                        "scenarios": [
                            {
                                "cost_multiplier": scenario.cost_multiplier,
                                "fold_count": scenario.fold_count,
                                "trade_count": scenario.trade_count,
                                "winning_trades": scenario.winning_trades,
                                "win_rate_pct": scenario.win_rate_pct,
                                "total_gross_pnl": scenario.total_gross_pnl,
                                "total_estimated_costs": scenario.total_estimated_costs,
                                "total_net_pnl": scenario.total_net_pnl,
                                "mean_net_return_pct": scenario.mean_net_return_pct,
                                "mean_excess_return_pct": scenario.mean_excess_return_pct,
                                "positive_fold_pct": scenario.positive_fold_pct,
                                "maximum_fold_drawdown_pct": scenario.maximum_fold_drawdown_pct,
                                "orders_authorized": False,
                                "holdout_used": False,
                            }
                            for scenario in cost_sensitivity.scenarios
                        ],
                        "orders_authorized": False,
                        "holdout_used": False,
                    }
                    quality_summary = {
                        "status": quality.status,
                        "fold_count": quality.fold_count,
                        "total_validation_trades": quality.total_validation_trades,
                        "mean_excess_return_pct": quality.mean_excess_return_pct,
                        "positive_fold_pct": quality.positive_fold_pct,
                        "maximum_fold_drawdown_pct": quality.maximum_fold_drawdown_pct,
                        "failure_reasons": list(quality.failure_reasons),
                        "holdout_used": False,
                        "orders_authorized": False,
                    }
                    candidate_definition = {
                        "candidate_id": candidate_id,
                        "family": rule_spec.family,
                        "parameters": dict(rule_spec.parameters),
                        "exit_policy": rule_spec.exit_policy,
                        "orders_authorized": False,
                    }
                    fingerprint_material = {
                        "research_version": _RESEARCH_VERSION,
                        "candidate": candidate_definition,
                        "symbol": symbol,
                        "dataset_manifest": dataset_manifest,
                        "market_data_manifest": manifest,
                        "walk_forward": {
                            "train_bars": wf_config.train_bars,
                            "validation_bars": wf_config.validation_bars,
                            "holdout_bars": wf_config.holdout_bars,
                            "step_bars": wf_config.step_bars,
                            "candle_interval_minutes": wf_config.candle_interval_minutes,
                        },
                        "backtest": {
                            "initial_equity": bt_config.initial_equity,
                            "position_fraction": bt_config.position_fraction,
                            "fee_bps_per_side": bt_config.fee_bps_per_side,
                            "slippage_bps_per_side": bt_config.slippage_bps_per_side,
                            "funding_bps_per_8h": bt_config.funding_bps_per_8h,
                            "minimum_signal_score": bt_config.minimum_signal_score,
                        },
                        "cost_sensitivity_multipliers": [1.0, 1.5, 2.0],
                    }
                    fingerprint = _canonical_digest(fingerprint_material)
                    folds = tuple(_window_summary(fold, used_for_selection=False) for fold in result.folds)
                    holdout = _window_summary(result.holdout, used_for_selection=False)
                    assumptions = {
                        "research_version": _RESEARCH_VERSION,
                        "strategy_candidate": candidate_definition,
                        "dataset_manifest": dataset_manifest,
                        "market_data_manifest": manifest,
                        "walk_forward": fingerprint_material["walk_forward"],
                        "backtest": fingerprint_material["backtest"],
                        "selection_policy": "four pre-registered fixed-rule families; validation folds only; untouched holdout excluded; no automatic promotion",
                        "orders_authorized": False,
                    }
                    outcomes = {
                        "folds": list(folds),
                        "holdout": holdout,
                        "validation_folds": list(folds),
                        "untouched_holdout": holdout,
                        "holdout_used_for_selection": False,
                        "validation_quality": quality_summary,
                        "cost_sensitivity": cost_sensitivity_summary,
                        "warnings": list(result.warnings),
                        "orders_authorized": False,
                    }
                    experiment = StrategyResearchExperiment(
                        experiment_id=f"solo-exp-{fingerprint[:32]}",
                        candidate_id=f"solo-{candidate_id}:{symbol}",
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
                    skipped.append({
                        "symbol": symbol,
                        "candidate": candidate_id,
                        "reason": f"{type(exc).__name__}: {str(exc)}"[:180],
                    })
                    logger.warning(
                        "strategy research candidate skipped symbol=%s candidate=%s reason=%s detail=%s orders_authorized=False",
                        symbol, candidate_id, type(exc).__name__, str(exc)[:120],
                    )
                    continue

                # Persistence is deliberately outside the candidate-validation catch:
                # any database error, including ValueError/TypeError, must fail the job
                # so the durable scheduler retries instead of recording a false success.
                memory.append(
                    experiment,
                    idempotency_key=f"solo-research:{fingerprint}",
                )
                evaluated.append(f"{candidate_id}:{symbol}")
                evaluated_symbols.add(symbol)

        if not evaluated:
            raise RuntimeError("strategy research completed; no candidates were evaluated")
        summary = {
            "evaluated": len(evaluated_symbols),
            "candidate_evaluations": len(evaluated),
            "symbols": sorted(evaluated_symbols),
            "candidates": [candidate_id for candidate_id, _ in _RESEARCH_CANDIDATES],
            "skipped": skipped,
            "orders_authorized": False,
            "job_idempotency_key": idempotency_key,
        }
        logger.info(
            "strategy research cycle completed symbols=%d candidate_evaluations=%d skipped=%d venue=%s orders_authorized=False",
            len(evaluated_symbols), len(evaluated), len(skipped), venue,
        )
        return summary
    return handle
