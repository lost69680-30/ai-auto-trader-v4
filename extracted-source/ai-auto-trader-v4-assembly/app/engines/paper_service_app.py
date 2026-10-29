"""Concrete, configuration-driven, paper-only application composition.

This module wires public market polling to research, ATR planning, signal
validation/watch, independent risk sizing, a SQLite paper ledger and protection.
It deliberately exposes no exchange order API. Exchange filters and cost/risk
assumptions must be present; unknown contract metadata blocks the symbol.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import hashlib
import logging
import math
import os
from pathlib import Path
import signal
import threading
from typing import Callable, Mapping, Protocol

from app.core.models import RiskLimits
from app.engines.complete_analysis_pipeline import AnalysisPipelineReport, CompleteAnalysisPipeline
from app.engines.orchestrator import TradingOrchestrator
from app.engines.paper_ledger import SQLitePaperLedger
from app.engines.postgres_paper_ledger import PostgresPaperLedger
from app.engines.paper_runtime import PaperRuntime, PaperRuntimeReport
from app.engines.paper_signal_cycle import PaperSignalCycle
from app.engines.paper_sizing import build_paper_orchestration_request
from app.engines.price_planner import PricePlanRequest, PricePlanningConfig
from app.engines.protection import PaperProtectionEngine
from app.engines.research_pipeline import ResearchPipelineResult, analyze_research_universe
from backend.liquidity_structure import Candle as LiquidityCandle, LiquidityConfig, MarketStructureInput
from backend.market_data_adapter import (
    BinanceUsdMFuturesAdapter, FundingRateRecord, MarketCandle, MarketUniverse,
)
from backend.market_worker import (
    FeedStatus, MarketDataWorker, MarketPollResult, MarketWorkerConfig,
)
from backend.opportunity_intelligence import OpportunityConfig
from backend.scanner import Contract, MarketSnapshot, ScannerConfig
from backend.signal_watcher import MarketTick, SignalWatcher, WatcherConfig
from backend.strategy_research import Candle as StrategyCandle, ResearchConfig

logger = logging.getLogger(__name__)


def _parse_market_testnet(value: str) -> bool:
    """Parse venue selection strictly so a typo cannot silently switch hosts."""
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise ValueError("PAPER_MARKET_TESTNET must be one of true/false, yes/no, or 1/0")


class MarketUniverseSource(Protocol):
    def fetch_market_universe(self, *, now: datetime | None = None) -> MarketUniverse: ...

    def fetch_klines(self, symbol: str, *, interval: str, limit: int,
                     now: datetime | None = None) -> tuple[MarketCandle, ...]: ...

    def fetch_mark_price(self, symbol: str): ...


@dataclass(frozen=True, slots=True)
class PaperSymbolPolicy:
    symbol: str
    estimated_round_trip_cost_pct: float
    max_mark_last_divergence_pct: float = 0.5

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("paper symbol must be non-empty, trimmed, and uppercase")
        for name in ("estimated_round_trip_cost_pct", "max_mark_last_divergence_pct"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if not 0 <= self.estimated_round_trip_cost_pct <= 1.0:
            raise ValueError("estimated round-trip cost must be between 0 and 1 percent")
        if not 0 <= self.max_mark_last_divergence_pct <= 10:
            raise ValueError("mark/last-price divergence cap must be between 0 and 10 percent")


@dataclass(frozen=True, slots=True)
class PaperServiceConfig:
    database_path: str
    symbols: tuple[PaperSymbolPolicy, ...]
    simulated_equity: float
    risk_fraction: float
    limits: RiskLimits
    fee_rate: float
    slippage_bps: float
    poll_interval_seconds: float = 5.0
    candle_interval: str = "1m"
    candle_limit: int = 100
    max_market_age_seconds: float = 90.0
    plan_lifetime_seconds: float = 20.0
    watcher_max_age_seconds: int = 10
    trailing_distance_pct: float | None = 0.02
    trailing_activation_pct: float = 0.01
    # No margin/leverage model exists in the paper ledger; default gross exposure
    # must therefore remain within simulated equity.
    max_gross_exposure: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.database_path, str) or not self.database_path.strip() or self.database_path == ":memory:":
            raise ValueError("database_path must be a file path for persistent paper state")
        if not self.symbols or len(self.symbols) > 20:
            raise ValueError("configure between 1 and 20 allowlisted paper symbols")
        names = [policy.symbol for policy in self.symbols]
        if len(names) != len(set(names)):
            raise ValueError("paper symbols must be unique")
        if not isinstance(self.limits, RiskLimits):
            raise ValueError("limits must be a RiskLimits instance")
        for name, value in (
            ("simulated_equity", self.simulated_equity), ("risk_fraction", self.risk_fraction),
            ("fee_rate", self.fee_rate), ("slippage_bps", self.slippage_bps),
            ("poll_interval_seconds", self.poll_interval_seconds),
            ("max_market_age_seconds", self.max_market_age_seconds),
            ("plan_lifetime_seconds", self.plan_lifetime_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if self.simulated_equity <= 0:
            raise ValueError("simulated_equity must be positive")
        if not 0 < self.risk_fraction <= 0.02:
            raise ValueError("risk_fraction must be greater than 0 and no more than 2 percent")
        if not 0 <= self.fee_rate <= 1 or not 0 <= self.slippage_bps < 10_000:
            raise ValueError("paper fee/slippage settings are outside valid ranges")
        if self.poll_interval_seconds < 1:
            raise ValueError("poll_interval_seconds must be at least 1 second")
        if self.max_market_age_seconds < 1 or self.plan_lifetime_seconds <= 0:
            raise ValueError("market age must be at least 1 second and plan lifetime positive")
        # Signal validation independently rejects data older than 30 seconds.
        # Keep plan expiry at or below that same boundary to avoid a dead window
        # where a watcher remains alive after its signal can no longer be revalidated.
        if self.plan_lifetime_seconds > min(30.0, self.max_market_age_seconds):
            raise ValueError("plan lifetime cannot exceed the stricter market-data freshness window")
        if isinstance(self.candle_limit, bool) or not isinstance(self.candle_limit, int) or not 30 <= self.candle_limit <= 500:
            raise ValueError("candle_limit must be between 30 and 500")
        if isinstance(self.watcher_max_age_seconds, bool) or not isinstance(self.watcher_max_age_seconds, int) or self.watcher_max_age_seconds <= 0:
            raise ValueError("watcher_max_age_seconds must be a positive integer")
        if self.watcher_max_age_seconds > self.max_market_age_seconds:
            raise ValueError("watcher market age cannot exceed runtime market age")
        if self.trailing_distance_pct is not None:
            if isinstance(self.trailing_distance_pct, bool) or not isinstance(self.trailing_distance_pct, (int, float)) or not math.isfinite(self.trailing_distance_pct):
                raise ValueError("trailing_distance_pct must be finite or None")
            if not 0 < self.trailing_distance_pct < 1:
                raise ValueError("trailing_distance_pct must be a decimal fraction between 0 and 1")
        if isinstance(self.trailing_activation_pct, bool) or not isinstance(self.trailing_activation_pct, (int, float)) or not math.isfinite(self.trailing_activation_pct):
            raise ValueError("trailing_activation_pct must be finite")
        if not 0 <= self.trailing_activation_pct < 1:
            raise ValueError("trailing_activation_pct must be a decimal fraction between 0 and 1")
        numeric_limits = (self.limits.max_position_notional, self.limits.max_daily_loss,
                          self.limits.max_risk_per_trade, self.limits.max_signal_age_seconds)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0
               for value in numeric_limits):
            raise ValueError("risk limits must be finite and positive")
        if isinstance(self.limits.max_open_positions, bool) or not isinstance(self.limits.max_open_positions, int) or self.limits.max_open_positions < 1:
            raise ValueError("max_open_positions must be a positive integer")
        if self.limits.max_daily_loss > self.simulated_equity * 0.02:
            raise ValueError("max_daily_loss cannot exceed 2 percent of the formal simulated-equity baseline")
        if self.limits.max_position_notional > self.simulated_equity * 0.10:
            raise ValueError("max_position_notional cannot exceed 10 percent of the formal simulated-equity baseline")
        if self.limits.max_position_notional > self.simulated_equity:
            raise ValueError("max_position_notional cannot exceed equity without an explicit leverage model")
        if self.limits.max_risk_per_trade > self.simulated_equity:
            raise ValueError("max_risk_per_trade cannot exceed simulated equity")
        if self.max_gross_exposure is not None:
            if (isinstance(self.max_gross_exposure, bool)
                    or not isinstance(self.max_gross_exposure, (int, float))
                    or not math.isfinite(self.max_gross_exposure)
                    or self.max_gross_exposure <= 0
                    or self.max_gross_exposure > self.simulated_equity):
                raise ValueError("max_gross_exposure must be positive and no greater than simulated equity")
        if self.risk_fraction * self.simulated_equity <= 0:
            raise ValueError("risk budget must be positive")

    @property
    def symbols_list(self) -> tuple[str, ...]:
        return tuple(policy.symbol for policy in self.symbols)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "PaperServiceConfig":
        """Load explicit paper risk settings; never guesses equity or risk limits."""
        env = os.environ if environ is None else environ
        required = (
            "PAPER_SYMBOLS", "PAPER_SIMULATED_EQUITY", "PAPER_RISK_FRACTION",
            "PAPER_MAX_POSITION_NOTIONAL", "PAPER_MAX_DAILY_LOSS", "PAPER_MAX_OPEN_POSITIONS",
            "PAPER_MAX_RISK_PER_TRADE", "PAPER_ESTIMATED_ROUND_TRIP_COST_PCT",
            "PAPER_FEE_RATE", "PAPER_SLIPPAGE_BPS",
        )
        missing = [name for name in required if not str(env.get(name, "")).strip()]
        if missing:
            raise ValueError("missing required paper configuration: " + ", ".join(missing))

        symbols = tuple(name.strip().upper() for name in env["PAPER_SYMBOLS"].split(",") if name.strip())
        cost = float(env["PAPER_ESTIMATED_ROUND_TRIP_COST_PCT"])
        return cls(
            database_path=env.get("PAPER_DATABASE_PATH", "./data/paper-ledger.sqlite3"),
            symbols=tuple(PaperSymbolPolicy(symbol, cost) for symbol in symbols),
            simulated_equity=float(env["PAPER_SIMULATED_EQUITY"]),
            risk_fraction=float(env["PAPER_RISK_FRACTION"]),
            limits=RiskLimits(
                max_position_notional=float(env["PAPER_MAX_POSITION_NOTIONAL"]),
                max_daily_loss=float(env["PAPER_MAX_DAILY_LOSS"]),
                max_open_positions=int(env["PAPER_MAX_OPEN_POSITIONS"]),
                max_risk_per_trade=float(env["PAPER_MAX_RISK_PER_TRADE"]),
                max_signal_age_seconds=float(env.get("PAPER_MAX_SIGNAL_AGE_SECONDS", "60")),
            ),
            fee_rate=float(env["PAPER_FEE_RATE"]),
            slippage_bps=float(env["PAPER_SLIPPAGE_BPS"]),
            poll_interval_seconds=float(env.get("PAPER_POLL_INTERVAL_SECONDS", "5")),
            candle_interval=env.get("PAPER_CANDLE_INTERVAL", "1m"),
            candle_limit=int(env.get("PAPER_CANDLE_LIMIT", "100")),
            max_market_age_seconds=float(env.get("PAPER_MAX_MARKET_AGE_SECONDS", "90")),
            plan_lifetime_seconds=float(env.get("PAPER_PLAN_LIFETIME_SECONDS", "20")),
            watcher_max_age_seconds=int(env.get("PAPER_WATCHER_MAX_AGE_SECONDS", "10")),
            trailing_distance_pct=float(env.get("PAPER_TRAILING_DISTANCE_PCT", "0.02")),
            trailing_activation_pct=float(env.get("PAPER_TRAILING_ACTIVATION_PCT", "0.01")),
            max_gross_exposure=float(env.get("PAPER_MAX_GROSS_EXPOSURE", env["PAPER_SIMULATED_EQUITY"])),
        )


@dataclass(frozen=True, slots=True)
class FundingSyncReport:
    status: str
    events_seen: int = 0
    events_recorded: int = 0
    events_duplicate: int = 0
    events_skipped_missing_mark: int = 0
    reason: str = "OK"


@dataclass(frozen=True, slots=True)
class PaperServiceAnalysisResult:
    status: str
    reason: str
    research_result: ResearchPipelineResult | None
    pipeline_report: AnalysisPipelineReport | None
    excluded_symbols: tuple[tuple[str, str], ...] = ()
    orders_authorized: bool = False
    funding_sync: FundingSyncReport | None = None


class PaperServiceAnalysisHandler:
    """Convert each safe market poll into real scanner/research/planning calls."""

    def __init__(
        self,
        *,
        source: MarketUniverseSource,
        config: PaperServiceConfig,
        ledger: SQLitePaperLedger,
        signal_cycle: PaperSignalCycle,
        clock: Callable[[], datetime],
    ) -> None:
        self._source = source
        self._config = config
        self._ledger = ledger
        self._cycle = signal_cycle
        self._clock = clock
        self._policy = {item.symbol: item for item in config.symbols}
        self._previous_funding: dict[str, float] = {}
        self._plan_request_cache: dict[tuple[str, str, datetime], PricePlanRequest] = {}
        self._last_funding_sync_at: datetime | None = None
        self._last_funding_sync_report: FundingSyncReport | None = None

    @staticmethod
    def _aware(value: object) -> bool:
        return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None

    @staticmethod
    def _valid_contract(contract: Contract) -> bool:
        values = (contract.tick_size, contract.quantity_step, contract.min_quantity)
        return all(isinstance(value, (int, float)) and not isinstance(value, bool)
                   and math.isfinite(value) and value > 0 for value in values)

    @staticmethod
    def _finite_number(value: object) -> bool:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value))

    def _sync_funding_events(self, *, now: datetime) -> FundingSyncReport:
        """Reconcile public historical funding into paper positions, never live accounts.

        Funding events are idempotent per position and exchange funding timestamp.
        Missing historical mark prices are not estimated from current prices.
        """
        if not self._aware(now):
            return FundingSyncReport("ERROR", reason="InvalidClock")
        now = now.astimezone(timezone.utc)
        if (self._last_funding_sync_at is not None and self._last_funding_sync_report is not None
                and 0 <= (now - self._last_funding_sync_at).total_seconds() < 60):
            return self._last_funding_sync_report

        positions = tuple(self._ledger.positions)
        if not positions:
            report = FundingSyncReport("NO_POSITIONS")
            self._last_funding_sync_at = None
            self._last_funding_sync_report = report
            return report

        fetch_history = getattr(self._source, "fetch_funding_rate_history", None)
        if not callable(fetch_history):
            report = FundingSyncReport("ERROR", reason="FundingHistoryUnavailable")
            self._last_funding_sync_at = now
            self._last_funding_sync_report = report
            return report

        recorded_events = tuple(self._ledger.funding_events)
        latest_by_position: dict[str, float] = {}
        for event in recorded_events:
            proposal_id = event.get("proposal_id")
            stamp = event.get("occurred_at")
            if isinstance(proposal_id, str) and self._finite_number(stamp):
                latest_by_position[proposal_id] = max(latest_by_position.get(proposal_id, -1.0), float(stamp))

        events_seen = events_recorded = events_duplicate = events_skipped_missing_mark = 0
        try:
            symbols = sorted({position.symbol for position in positions if position.symbol in self._policy})
            for symbol in symbols:
                symbol_positions = tuple(position for position in positions if position.symbol == symbol)
                cursors = []
                for position in symbol_positions:
                    cursor = max(float(position.opened_at), latest_by_position.get(position.proposal_id, float(position.opened_at) - 0.001) + 0.001)
                    cursors.append(cursor)
                start_ms = max(0, int(min(cursors) * 1000))
                end_ms = int(now.timestamp() * 1000)
                pages = 0
                while start_ms <= end_ms:
                    rows = fetch_history(
                        symbol, start_time_ms=start_ms, end_time_ms=end_ms, limit=1000,
                    )
                    if not isinstance(rows, (tuple, list)):
                        raise ValueError("MalformedFundingHistory")
                    if not rows:
                        break
                    pages += 1
                    if pages > 10:
                        raise ValueError("FundingHistoryBacklogTooLarge")
                    last_stamp_ms = None
                    for event in rows:
                        if not isinstance(event, FundingRateRecord):
                            raise ValueError("MalformedFundingRecord")
                        if event.symbol != symbol or event.orders_authorized is not False:
                            raise ValueError("FundingRecordRejected")
                        if not self._aware(event.funding_time):
                            raise ValueError("InvalidFundingTimestamp")
                        event_time = event.funding_time.astimezone(timezone.utc)
                        stamp_ms = int(event_time.timestamp() * 1000)
                        if stamp_ms < start_ms or stamp_ms > end_ms or event_time > now:
                            raise ValueError("FundingTimestampOutOfRange")
                        if last_stamp_ms is not None and stamp_ms <= last_stamp_ms:
                            raise ValueError("FundingHistoryNotStrictlyIncreasing")
                        last_stamp_ms = stamp_ms
                        if (isinstance(event.funding_rate, bool)
                                or not self._finite_number(event.funding_rate)):
                            raise ValueError("InvalidFundingRate")

                        eligible_positions = tuple(
                            position for position in symbol_positions
                            if float(position.opened_at) <= event_time.timestamp()
                            and (position.closed_at is None or event_time.timestamp() <= float(position.closed_at))
                        )
                        for position in eligible_positions:
                            events_seen += 1
                            mark = event.mark_price
                            if mark is None:
                                events_skipped_missing_mark += 1
                                continue
                            if (isinstance(mark, bool) or not self._finite_number(mark) or float(mark) <= 0):
                                raise ValueError("InvalidHistoricalFundingMark")
                            funding_id = f"public-funding:{symbol}:{stamp_ms}:{position.proposal_id}"
                            result = self._ledger.record_funding_payment(
                                position.proposal_id,
                                funding_id=funding_id,
                                funding_rate=float(event.funding_rate),
                                mark_price=float(mark),
                                occurred_at=event_time.timestamp(),
                            )
                            if result == "RECORDED":
                                events_recorded += 1
                            elif result == "DUPLICATE":
                                events_duplicate += 1
                            else:
                                raise ValueError("FundingLedgerConflict")
                    if len(rows) < 1000:
                        break
                    if last_stamp_ms is None or last_stamp_ms < start_ms:
                        raise ValueError("FundingPaginationDidNotAdvance")
                    start_ms = last_stamp_ms + 1
            status = "DEGRADED" if events_skipped_missing_mark else "OK"
            reason = "HistoricalFundingMarkMissing" if events_skipped_missing_mark else "OK"
            report = FundingSyncReport(
                status, events_seen, events_recorded, events_duplicate,
                events_skipped_missing_mark, reason,
            )
        except Exception as exc:
            report = FundingSyncReport(
                "ERROR", events_seen, events_recorded, events_duplicate,
                events_skipped_missing_mark, type(exc).__name__,
            )
        self._last_funding_sync_at = now
        self._last_funding_sync_report = report
        return report

    def __call__(self, poll: MarketPollResult) -> PaperServiceAnalysisResult:
        now = self._clock()
        if not self._aware(now):
            raise ValueError("paper service clock must be timezone-aware")
        now = now.astimezone(timezone.utc)
        if poll.orders_authorized is not False:
            return PaperServiceAnalysisResult("AUTHORIZATION_FLAG_REJECTED", "Poll cannot authorize orders.", None, None)

        frames_by_symbol: dict[str, object] = {}
        excluded: list[tuple[str, str]] = []
        allowlisted_frames = [frame for frame in poll.frames if getattr(frame, "symbol", None) in self._policy]
        frame_counts: dict[str, int] = {}
        for frame in allowlisted_frames:
            frame_counts[frame.symbol] = frame_counts.get(frame.symbol, 0) + 1
        for frame in allowlisted_frames:
            if frame_counts[frame.symbol] != 1:
                # Reject every copy, including a third or later duplicate.
                if frame.symbol not in {symbol for symbol, reason in excluded if reason == "duplicate symbol frame"}:
                    excluded.append((frame.symbol, "duplicate symbol frame"))
                continue
            if frame.orders_authorized is not False or frame.status is not FeedStatus.HEALTHY:
                excluded.append((frame.symbol, "frame is not healthy and paper-only"))
                continue
            if (not self._aware(frame.mark_observed_at)
                    or isinstance(frame.mark_price, bool)
                    or not isinstance(frame.mark_price, (int, float))
                    or not math.isfinite(frame.mark_price) or frame.mark_price <= 0):
                excluded.append((frame.symbol, "mark price or timestamp is invalid"))
                continue
            frames_by_symbol[frame.symbol] = frame
        if not frames_by_symbol:
            return PaperServiceAnalysisResult("NO_HEALTHY_ALLOWLISTED_SYMBOLS", "No healthy allowlisted symbols were supplied.", None, None, tuple(excluded))

        try:
            universe = self._source.fetch_market_universe(now=now)
        except Exception as exc:
            return PaperServiceAnalysisResult("MARKET_UNIVERSE_UNAVAILABLE", f"Public market-universe retrieval failed ({type(exc).__name__}).", None, None, tuple(excluded))
        if not isinstance(universe, MarketUniverse) or universe.orders_authorized is not False:
            return PaperServiceAnalysisResult("MARKET_UNIVERSE_REJECTED", "Universe contract or authorization flag is invalid.", None, None, tuple(excluded))
        if not self._aware(universe.fetched_at):
            return PaperServiceAnalysisResult("MARKET_UNIVERSE_REJECTED", "Universe timestamp is not timezone-aware.", None, None, tuple(excluded))
        universe_age = (now - universe.fetched_at.astimezone(timezone.utc)).total_seconds()
        if universe_age < 0 or universe_age > self._config.max_market_age_seconds:
            return PaperServiceAnalysisResult("MARKET_UNIVERSE_STALE", "Market universe is stale or future-dated.", None, None, tuple(excluded))

        contract_counts: dict[str, int] = {}
        snapshot_counts: dict[str, int] = {}
        for row in universe.contracts:
            contract_counts[row.symbol] = contract_counts.get(row.symbol, 0) + 1
        for row in universe.snapshots:
            snapshot_counts[row.symbol] = snapshot_counts.get(row.symbol, 0) + 1
        contract_by_symbol = {row.symbol: row for row in universe.contracts if contract_counts[row.symbol] == 1}
        snapshot_by_symbol = {row.symbol: row for row in universe.snapshots if snapshot_counts[row.symbol] == 1}

        contracts: list[Contract] = []
        snapshots: list[MarketSnapshot] = []
        candles_by_symbol: dict[str, tuple[StrategyCandle, ...]] = {}
        structure_inputs: list[MarketStructureInput] = []
        frame_by_symbol: dict[str, object] = {}
        ticks: dict[str, MarketTick] = {}
        for symbol, frame in frames_by_symbol.items():
            contract = contract_by_symbol.get(symbol)
            snapshot = snapshot_by_symbol.get(symbol)
            if contract is None or contract_counts.get(symbol, 0) != 1:
                excluded.append((symbol, "contract missing or duplicated in exchange metadata"))
                continue
            if not self._valid_contract(contract):
                excluded.append((symbol, "exchange tick/step/minimum-quantity filters are missing or invalid"))
                continue
            if snapshot is None or snapshot_counts.get(symbol, 0) != 1:
                excluded.append((symbol, "market snapshot missing or duplicated"))
                continue
            if not self._aware(snapshot.observed_at):
                excluded.append((symbol, "market snapshot timestamp is invalid"))
                continue
            snapshot_values = (snapshot.last_price, snapshot.quote_volume_24h,
                               snapshot.spread_bps, snapshot.volatility_pct)
            if (not all(self._finite_number(value) for value in snapshot_values)
                    or snapshot.last_price <= 0 or snapshot.quote_volume_24h < 0
                    or snapshot.spread_bps < 0 or snapshot.volatility_pct < 0):
                excluded.append((symbol, "market snapshot contains invalid numeric fields"))
                continue
            snapshot_age = (now - snapshot.observed_at.astimezone(timezone.utc)).total_seconds()
            if snapshot_age < 0 or snapshot_age > self._config.max_market_age_seconds:
                excluded.append((symbol, "market snapshot is stale or future-dated"))
                continue
            divergence_pct = abs(snapshot.last_price - frame.mark_price) / frame.mark_price * 100.0
            policy = self._policy[symbol]
            if not math.isfinite(divergence_pct) or divergence_pct > policy.max_mark_last_divergence_pct:
                excluded.append((symbol, "last-price/mark-price divergence exceeds configured limit"))
                continue
            if contract.status != "TRADING" or contract.contract_type != "PERPETUAL" or contract.quote_asset != "USDT":
                excluded.append((symbol, "contract is not an eligible USDT perpetual"))
                continue
            contracts.append(contract)
            snapshots.append(snapshot)
            frame_by_symbol[symbol] = frame
            ticks[symbol] = MarketTick(
                symbol=symbol, price=float(frame.mark_price), observed_at=frame.mark_observed_at,
                candle_close=frame.closed_candles[-1].close if frame.closed_candles else None,
            )
            rows = tuple(
                StrategyCandle(
                    symbol=symbol, timestamp=candle.close_time,
                    open=candle.open, high=candle.high, low=candle.low,
                    close=candle.close, volume=candle.volume,
                )
                for candle in frame.closed_candles if candle.is_closed
            )
            if rows:
                candles_by_symbol[symbol] = rows
            funding = getattr(frame, "funding_rate", None)
            if rows and isinstance(funding, (int, float)) and not isinstance(funding, bool) and math.isfinite(funding):
                prior_funding = self._previous_funding.get(symbol, float(funding))
                liq_rows = tuple(LiquidityCandle(r.open, r.high, r.low, r.close, r.volume) for r in rows)
                baseline_values = [r.volume for r in liq_rows[-21:-1]]
                previous_rows = liq_rows[-5:-1]
                if baseline_values and previous_rows and sum(baseline_values) > 0:
                    prior_high = max(r.high for r in previous_rows)
                    prior_low = min(r.low for r in previous_rows)
                    structure_inputs.append(MarketStructureInput(
                        symbol=symbol,
                        candles=liq_rows,
                        timestamp=frame.mark_observed_at.astimezone(timezone.utc),
                        reference_level=(prior_high + prior_low) / 2.0,
                        previous_close=liq_rows[-2].close,
                        current_funding_rate=float(funding),
                        previous_funding_rate=prior_funding,
                        volume_baseline=sum(baseline_values) / len(baseline_values),
                        estimated_round_trip_cost_pct=policy.estimated_round_trip_cost_pct,
                    ))
                self._previous_funding[symbol] = float(funding)

        if not contracts or not snapshots:
            empty = ResearchPipelineResult((), (), orders_authorized=False)
            return PaperServiceAnalysisResult("NO_ELIGIBLE_SYMBOLS", "No symbol passed market metadata and freshness checks.", empty, None, tuple(excluded))

        research_result = analyze_research_universe(
            contracts=contracts,
            market_snapshots=snapshots,
            candles_by_symbol=candles_by_symbol,
            structure_inputs=structure_inputs,
            now=now,
            scanner_config=ScannerConfig(max_data_age_seconds=int(self._config.max_market_age_seconds)),
            opportunity_config=OpportunityConfig(max_signal_age_seconds=int(self._config.max_market_age_seconds)),
            research_config=ResearchConfig(max_candle_age_seconds=int(self._config.max_market_age_seconds)),
            liquidity_config=LiquidityConfig(max_data_age_seconds=int(self._config.max_market_age_seconds)),
        )

        def plan_request_factory(candidate: object) -> PricePlanRequest:
            symbol = candidate.symbol
            frame = frame_by_symbol[symbol]
            contract = contract_by_symbol[symbol]
            candles = tuple(c for c in frame.closed_candles if c.is_closed)
            if not candles:
                raise ValueError("no closed candles for price planning")
            candle_close = candles[-1].close_time.astimezone(timezone.utc)
            cache_key = (symbol, candidate.direction, candle_close)
            cached = self._plan_request_cache.get(cache_key)
            if cached is not None:
                return cached
            signal_id = "paper-" + hashlib.sha256(
                f"{symbol}|{candidate.direction}|{candle_close.isoformat()}".encode()
            ).hexdigest()[:24]
            request = PricePlanRequest(
                signal_id=signal_id,
                symbol=symbol,
                direction=candidate.direction,
                reference_price=float(frame.mark_price),
                reference_price_at=frame.mark_observed_at,
                candles=candles,
                now=now,
                expires_at=now + timedelta(seconds=self._config.plan_lifetime_seconds),
                estimated_round_trip_cost_pct=self._policy[symbol].estimated_round_trip_cost_pct,
                tick_size=float(contract.tick_size),
            )
            self._plan_request_cache[cache_key] = request
            # Bound cache growth without deleting recent keys used by active watches.
            if len(self._plan_request_cache) > 500:
                ordered = sorted(self._plan_request_cache.items(), key=lambda pair: pair[0][2])
                for old_key, _ in ordered[:len(ordered) - 300]:
                    self._plan_request_cache.pop(old_key, None)
            return request

        def orchestration_request_factory(candidate: object, handoff: object):
            symbol = candidate.symbol
            position_rows = self._ledger.positions
            day_start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc).timestamp()
            realized_today = sum(
                float(position.realized_pnl or 0.0)
                for position in position_rows
                if position.status == "CLOSED" and position.closed_at is not None and position.closed_at >= day_start
            )
            open_rows = self._ledger.open_positions
            open_unrealized = sum(position.unrealized_pnl - position.opening_fee for position in open_rows)
            realized_all = sum(float(position.realized_pnl or 0.0) for position in position_rows if position.status == "CLOSED")
            equity = self._config.simulated_equity + realized_all + open_unrealized
            daily_pnl = realized_today + open_unrealized
            if not math.isfinite(equity) or equity <= 0:
                raise ValueError("simulated equity exhausted or invalid")
            exposure = sum(position.mark_price * position.quantity for position in open_rows)
            gross_cap = min(
                self._config.max_gross_exposure or self._config.simulated_equity,
                self._config.simulated_equity * 1.0,
            )
            remaining_exposure = gross_cap - exposure
            same_asset_exposure = sum(
                position.mark_price * position.quantity
                for position in open_rows if position.symbol.upper() == symbol.upper()
            )
            remaining_asset_allocation = self._config.simulated_equity * 0.10 - same_asset_exposure
            if not math.isfinite(remaining_exposure) or remaining_exposure <= 0:
                raise ValueError("gross exposure cap is exhausted; no additional position is allowed")
            if not math.isfinite(remaining_asset_allocation) or remaining_asset_allocation <= 0:
                raise ValueError("single-asset allocation cap is exhausted; no additional position is allowed")
            configured_limits = self._config.limits
            effective_limits = RiskLimits(
                max_position_notional=min(
                    configured_limits.max_position_notional,
                    remaining_exposure,
                    remaining_asset_allocation,
                ),
                max_daily_loss=configured_limits.max_daily_loss,
                max_open_positions=configured_limits.max_open_positions,
                max_risk_per_trade=configured_limits.max_risk_per_trade,
                max_signal_age_seconds=configured_limits.max_signal_age_seconds,
                max_drawdown_pct=configured_limits.max_drawdown_pct,
                max_mark_age_seconds=configured_limits.max_mark_age_seconds,
            )
            contract = contract_by_symbol[symbol]
            return build_paper_orchestration_request(
                candidate=candidate, handoff=handoff, tick=ticks[symbol], now=now,
                equity=equity, risk_fraction=self._config.risk_fraction,
                quantity_step=float(contract.quantity_step), min_quantity=float(contract.min_quantity),
                limits=effective_limits, daily_pnl=daily_pnl, open_positions=len(open_rows),
                max_tick_age_seconds=min(10.0, self._config.max_market_age_seconds),
                equity_baseline=self._config.simulated_equity,
            )

        cost_limit = max(policy.estimated_round_trip_cost_pct for policy in self._config.symbols)
        pipeline = CompleteAnalysisPipeline(
            plan_request_factory=plan_request_factory,
            orchestration_request_factory=orchestration_request_factory,
            signal_cycle=self._cycle,
            price_config=PricePlanningConfig(
                max_reference_age_seconds=min(30, int(self._config.max_market_age_seconds)),
                max_candle_age_seconds=int(self._config.max_market_age_seconds),
                max_round_trip_cost_pct=cost_limit,
            ),
        )
        pipeline_report = pipeline.process_research(research_result, ticks=ticks, now=now)
        status = "ANALYZED" if not excluded else "ANALYZED_WITH_EXCLUSIONS"
        return PaperServiceAnalysisResult(
            status=status,
            reason="Public market data was screened; research/planning output remains paper-only.",
            research_result=research_result,
            pipeline_report=pipeline_report,
            excluded_symbols=tuple(excluded),
            orders_authorized=False,
        )


class PaperServiceApplication:
    """Lifecycle owner for the configured worker, runtime, ledger and protection."""

    def __init__(self, *, config: PaperServiceConfig, worker: MarketDataWorker,
                 ledger: SQLitePaperLedger, protection: PaperProtectionEngine,
                 runtime: PaperRuntime[PaperServiceAnalysisResult]) -> None:
        self.config = config
        self.worker = worker
        self.ledger = ledger
        self.protection = protection
        self.runtime = runtime
        self._closed = False

    @classmethod
    def create(
        cls,
        *,
        source: MarketUniverseSource,
        config: PaperServiceConfig,
        clock: Callable[[], datetime] | None = None,
    ) -> "PaperServiceApplication":
        clock = clock or (lambda: datetime.now(timezone.utc))
        # An explicitly configured PostgreSQL URL is authoritative. Never create
        # a local SQLite ledger as a fallback if the URL is empty or PostgreSQL fails.
        database_url = os.environ.get("PAPER_DATABASE_URL")
        if database_url is not None:
            if not database_url.strip():
                raise RuntimeError("PAPER_DATABASE_URL is configured but empty; refusing local SQLite fallback.")
            ledger = PostgresPaperLedger(database_url.strip())
        else:
            path = Path(config.database_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            ledger = SQLitePaperLedger(path)
        try:
            ledger.verify_integrity()
            startup_stamp = clock()
            if not isinstance(startup_stamp, datetime) or startup_stamp.tzinfo is None or startup_stamp.utcoffset() is None:
                raise ValueError("service startup clock must return a timezone-aware datetime")
            # Trigger watches and cached plans are process-local. Never resume an
            # old pending intent after restart; cancel it durably and require fresh
            # market data plus a newly validated signal.
            ledger.cancel_pending_paper_orders(
                occurred_at=startup_stamp.timestamp(),
                reason="service restart: process-local trigger/price-plan state was not restored",
            )
            protection = PaperProtectionEngine(
                ledger,
                trailing_distance_pct=config.trailing_distance_pct,
                trailing_activation_pct=config.trailing_activation_pct,
                fee_rate=config.fee_rate,
                slippage_bps=config.slippage_bps,
            )
            watcher = SignalWatcher(WatcherConfig(
                max_market_data_age_seconds=config.watcher_max_age_seconds,
            ))
            cycle = PaperSignalCycle(TradingOrchestrator(ledger), watcher)
            handler = PaperServiceAnalysisHandler(
                source=source, config=config, ledger=ledger, signal_cycle=cycle, clock=clock,
            )
            worker_config = MarketWorkerConfig(
                symbols=config.symbols_list,
                interval=config.candle_interval,
                candle_limit=config.candle_limit,
                poll_interval_seconds=config.poll_interval_seconds,
                max_market_age_seconds=config.max_market_age_seconds,
            )
            worker = MarketDataWorker(source, worker_config)
            runtime = PaperRuntime(
                ledger=ledger, protection=protection, analysis_handler=handler,
                fee_rate=config.fee_rate, slippage_bps=config.slippage_bps,
                max_market_age_seconds=config.max_market_age_seconds,
            )
            return cls(config=config, worker=worker, ledger=ledger, protection=protection, runtime=runtime)
        except Exception:
            ledger.close()
            raise

    @classmethod
    def from_env(cls, *, environ: Mapping[str, str] | None = None,
                 clock: Callable[[], datetime] | None = None) -> "PaperServiceApplication":
        config = PaperServiceConfig.from_env(environ)
        env = os.environ if environ is None else environ
        testnet = _parse_market_testnet(env.get("PAPER_MARKET_TESTNET", "true"))
        # This adapter only uses public GET endpoints. Testnet here means market
        # data only; this application has no credentials or exchange-order client.
        source = BinanceUsdMFuturesAdapter(testnet=testnet)
        return cls.create(source=source, config=config, clock=clock)

    def process_poll(self, poll: MarketPollResult, *, now: float) -> PaperRuntimeReport[PaperServiceAnalysisResult]:
        if self._closed:
            raise RuntimeError("paper service application is closed")
        return self.runtime.process_poll(poll, now=now)

    def run_forever(self, stop_event: threading.Event, *,
                    on_report: Callable[[PaperRuntimeReport[PaperServiceAnalysisResult]], None] | None = None,
                    clock: Callable[[], datetime] | None = None) -> None:
        if self._closed:
            raise RuntimeError("paper service application is closed")
        self.runtime.run_forever(self.worker, stop_event, on_report=on_report, clock=clock)

    def activate_emergency_stop(self, *, reason: str, now: datetime | None = None) -> tuple[str, ...]:
        """Persist the paper emergency stop and cancel pending intents immediately."""
        stamp = (now or datetime.now(timezone.utc))
        if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("emergency-stop timestamp must be timezone-aware")
        result = self.protection.activate_emergency_stop(observed_at=stamp.timestamp(), reason=reason)
        return result.cancelled_order_ids

    def close(self) -> None:
        if not self._closed:
            self.ledger.close()
            self._closed = True


def main() -> None:
    """Run the public-data, paper-only service with explicit environment config."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = PaperServiceApplication.from_env()
    stop = threading.Event()

    def request_stop(signum, frame) -> None:
        logger.info("Shutdown requested; stopping paper market worker")
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)
    try:
        app.run_forever(stop, on_report=lambda report: logger.info(
            "paper cycle status=%s healthy=%s unhealthy=%s open_positions=%d orders_authorized=%s",
            report.analysis_status, report.healthy_symbols, report.unhealthy_symbols,
            report.open_positions, report.orders_authorized,
        ))
    finally:
        app.close()


if __name__ == "__main__":
    main()
