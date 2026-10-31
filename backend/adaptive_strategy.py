"""Deterministic adaptive strategy selection; research-only, never an order authority.

The selector ranks already-assessed strategy proposals against validated timeframe
availability, market regime, freshness, directional conflicts and execution-cost
limits. It does not calculate entry prices, size positions, call an exchange, or
authorize paper/live orders.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import math
from typing import Mapping, Sequence


class StrategyKind(str, Enum):
    SCALPING = "SCALPING"
    DAY = "DAY"
    SWING = "SWING"
    POSITION = "POSITION"
    LONG_TERM_ANALYSIS = "LONG_TERM_ANALYSIS"


class MarketRegime(str, Enum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True, slots=True)
class TimeframeState:
    interval: str
    direction: str
    observed_at: datetime
    healthy: bool
    closed: bool
    conflicts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StrategyProposal:
    strategy: StrategyKind
    direction: str
    eligible: bool
    ranking_score: float
    estimated_round_trip_cost_bps: float
    max_round_trip_cost_bps: float
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AdaptiveSelectionConfig:
    max_timeframe_age_seconds: float = 300.0
    max_future_skew_seconds: float = 2.0
    minimum_ranking_score: float = 60.0
    cost_penalty_per_bps: float = 0.1

    def __post_init__(self) -> None:
        values = (self.max_timeframe_age_seconds, self.max_future_skew_seconds,
                  self.minimum_ranking_score, self.cost_penalty_per_bps)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("adaptive selector configuration must be finite")
        if self.max_timeframe_age_seconds <= 0 or self.max_future_skew_seconds < 0:
            raise ValueError("timeframe freshness limits are invalid")
        if not 0 <= self.minimum_ranking_score <= 100 or self.cost_penalty_per_bps < 0:
            raise ValueError("ranking configuration is invalid")


@dataclass(frozen=True, slots=True)
class RankedStrategy:
    strategy: StrategyKind
    direction: str
    source_score: float
    estimated_round_trip_cost_bps: float
    adjusted_score: float
    evidence: tuple[str, ...]
    analysis_only: bool = False


@dataclass(frozen=True, slots=True)
class AdaptiveSelection:
    status: str
    symbol: str
    selected_strategy: StrategyKind | None
    direction: str
    ranked_candidates: tuple[RankedStrategy, ...]
    reasons: tuple[str, ...]
    analysis_only: bool
    orders_authorized: bool = False


# Primary intervals determine the setup direction; confirmation intervals may
# be neutral but must not explicitly oppose it. These are strategy contracts,
# not claims that any interval is statistically independent.
_STRATEGY_TIMEFRAMES: dict[StrategyKind, tuple[tuple[str, ...], tuple[str, ...]]] = {
    StrategyKind.SCALPING: (("1m", "5m"), ("15m", "1h")),
    StrategyKind.DAY: (("5m", "15m", "1h"), ("4h",)),
    StrategyKind.SWING: (("1h", "4h"), ("1d", "1w")),
    StrategyKind.POSITION: (("1d", "1w"), ("1M",)),
    StrategyKind.LONG_TERM_ANALYSIS: (("1w", "1M"), ("1Y",)),
}

_ALLOWED_REGIMES: dict[StrategyKind, frozenset[MarketRegime]] = {
    StrategyKind.SCALPING: frozenset({MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN, MarketRegime.RANGING}),
    StrategyKind.DAY: frozenset({MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN, MarketRegime.RANGING}),
    StrategyKind.SWING: frozenset({MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN}),
    StrategyKind.POSITION: frozenset({MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN}),
    StrategyKind.LONG_TERM_ANALYSIS: frozenset({
        MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN,
        MarketRegime.RANGING, MarketRegime.HIGH_VOLATILITY,
    }),
}

_STRATEGY_ORDER = {
    StrategyKind.SCALPING: 0,
    StrategyKind.DAY: 1,
    StrategyKind.SWING: 2,
    StrategyKind.POSITION: 3,
    StrategyKind.LONG_TERM_ANALYSIS: 4,
}


def _validate_proposal(proposal: StrategyProposal) -> None:
    if not isinstance(proposal.strategy, StrategyKind):
        raise ValueError("proposal strategy is unsupported")
    if proposal.direction not in {"LONG", "SHORT"}:
        raise ValueError("proposal direction must be LONG or SHORT")
    if not isinstance(proposal.eligible, bool):
        raise ValueError("proposal eligibility must be boolean")
    numbers = (proposal.ranking_score, proposal.estimated_round_trip_cost_bps,
               proposal.max_round_trip_cost_bps)
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
               and math.isfinite(value) for value in numbers):
        raise ValueError("proposal scores and costs must be finite numbers")
    if not 0 <= proposal.ranking_score <= 100:
        raise ValueError("proposal ranking_score must be between 0 and 100")
    if proposal.estimated_round_trip_cost_bps < 0 or proposal.max_round_trip_cost_bps < 0:
        raise ValueError("proposal costs must be non-negative")
    if not isinstance(proposal.evidence, tuple) or any(
        not isinstance(item, str) or not item.strip() for item in proposal.evidence
    ):
        raise ValueError("proposal evidence must be a tuple of non-empty strings")


def _timeframe_failure(
    interval: str,
    direction: str,
    states: Mapping[str, TimeframeState],
    *,
    now: datetime,
    config: AdaptiveSelectionConfig,
) -> str | None:
    state = states.get(interval)
    if state is None:
        return f"required timeframe {interval} is missing"
    if not isinstance(state, TimeframeState) or state.interval != interval:
        return f"required timeframe {interval} has an invalid state"
    if state.direction not in {"BULLISH", "BEARISH", "NEUTRAL", "UNKNOWN"}:
        return f"required timeframe {interval} has an invalid direction"
    if not isinstance(state.observed_at, datetime) or state.observed_at.tzinfo is None or state.observed_at.utcoffset() is None:
        return f"required timeframe {interval} has an invalid timestamp"
    if state.healthy is not True:
        return f"required timeframe {interval} is unhealthy"
    if state.closed is not True:
        return f"required timeframe {interval} is not based on a closed candle"
    if state.direction == "UNKNOWN":
        return f"required timeframe {interval} has unknown direction"
    age = (now - state.observed_at.astimezone(timezone.utc)).total_seconds()
    if age < -config.max_future_skew_seconds:
        return f"required timeframe {interval} is future-dated"
    if age > config.max_timeframe_age_seconds:
        return f"required timeframe {interval} is stale"
    if state.conflicts:
        return f"required timeframe {interval} reports conflicting evidence"
    opposing = "BEARISH" if direction == "LONG" else "BULLISH"
    if state.direction == opposing:
        return f"required timeframe {interval} conflicts with proposal direction"
    return None


def select_adaptive_strategy(
    *,
    symbol: str,
    regime: MarketRegime,
    timeframes: Mapping[str, TimeframeState],
    proposals: Sequence[StrategyProposal],
    now: datetime,
    config: AdaptiveSelectionConfig | None = None,
) -> AdaptiveSelection:
    """Select the best eligible proposal, or explicitly wait.

    Long-term analysis may be returned as ANALYSIS_ONLY with direction NONE.
    No returned state has order authority; every executable paper proposal still
    requires separate price planning, validation and independent risk approval.
    """
    if not isinstance(symbol, str) or not symbol.strip() or symbol != symbol.strip():
        raise ValueError("symbol must be a non-empty trimmed string")
    if not isinstance(regime, MarketRegime):
        raise ValueError("market regime is unsupported")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not isinstance(timeframes, Mapping):
        raise ValueError("timeframes must be a mapping")
    if not isinstance(proposals, Sequence):
        raise ValueError("proposals must be a sequence")
    config = config or AdaptiveSelectionConfig()
    now = now.astimezone(timezone.utc)

    eligible: list[RankedStrategy] = []
    rejected: list[str] = []
    seen: set[StrategyKind] = set()
    for proposal in proposals:
        if not isinstance(proposal, StrategyProposal):
            raise ValueError("all proposals must be StrategyProposal values")
        _validate_proposal(proposal)
        if proposal.strategy in seen:
            raise ValueError("duplicate strategy proposal")
        seen.add(proposal.strategy)

        if not proposal.eligible:
            rejected.append(f"{proposal.strategy.value}: upstream assessment marked it ineligible")
            continue
        if regime not in _ALLOWED_REGIMES[proposal.strategy]:
            rejected.append(f"{proposal.strategy.value}: incompatible with market regime {regime.value}")
            continue
        if proposal.estimated_round_trip_cost_bps > proposal.max_round_trip_cost_bps:
            rejected.append(f"{proposal.strategy.value}: estimated execution cost exceeds its limit")
            continue
        if proposal.ranking_score < config.minimum_ranking_score:
            rejected.append(f"{proposal.strategy.value}: ranking score is below the configured threshold")
            continue

        primary, confirmation = _STRATEGY_TIMEFRAMES[proposal.strategy]
        failure = None
        for interval in primary + confirmation:
            failure = _timeframe_failure(interval, proposal.direction, timeframes,
                                         now=now, config=config)
            if failure:
                break
        if failure:
            rejected.append(f"{proposal.strategy.value}: {failure}")
            continue

        adjusted = max(0.0, proposal.ranking_score -
                       proposal.estimated_round_trip_cost_bps * config.cost_penalty_per_bps)
        eligible.append(RankedStrategy(
            strategy=proposal.strategy,
            direction=proposal.direction,
            source_score=float(proposal.ranking_score),
            estimated_round_trip_cost_bps=float(proposal.estimated_round_trip_cost_bps),
            adjusted_score=round(adjusted, 6),
            evidence=proposal.evidence,
            analysis_only=proposal.strategy is StrategyKind.LONG_TERM_ANALYSIS,
        ))

    eligible.sort(key=lambda item: (-item.adjusted_score, _STRATEGY_ORDER[item.strategy]))
    if not eligible:
        reasons = tuple(rejected) or ("no strategy proposals were supplied",)
        return AdaptiveSelection("WAIT", symbol, None, "NONE", (), reasons, False, False)

    winner = eligible[0]
    if winner.analysis_only:
        return AdaptiveSelection(
            "ANALYSIS_ONLY", symbol, winner.strategy, "NONE", tuple(eligible),
            ("long-term strategy is analysis-only in the current release",), True, False,
        )
    return AdaptiveSelection(
        "SELECTED", symbol, winner.strategy, winner.direction, tuple(eligible),
        ("selected the highest-ranked eligible strategy; risk approval is still required",),
        False, False,
    )
