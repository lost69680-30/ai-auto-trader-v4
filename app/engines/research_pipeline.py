"""Connect market screening to research-only opportunity, strategy and liquidity analysis.

This layer deliberately stops before signal creation, validation, risk approval or
paper execution. It does not invent entry/stop/target prices from research scores.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping

from backend.liquidity_structure import (
    LiquidityConfig,
    MarketStructureInput,
    RankedOpportunity,
    rank_opportunities,
)
from backend.opportunity_intelligence import (
    OpportunityConfig,
    OpportunityReport,
    assess_opportunity,
)
from backend.scanner import (
    Candidate as ScanCandidate,
    Contract,
    MarketSnapshot as ScannerMarketSnapshot,
    Rejection,
    ScanResult,
    ScannerConfig,
    scan_candidates,
)
from backend.strategy_research import (
    Candle,
    ResearchConfig,
    ResearchResult,
    research_strategies,
)


@dataclass(frozen=True, slots=True)
class ResearchCandidate:
    symbol: str
    scanner_score: float
    status: str
    direction: str
    reasons: tuple[str, ...]
    opportunity: OpportunityReport | None
    strategy_research: ResearchResult | None
    liquidity: RankedOpportunity | None
    orders_authorized: bool = False


@dataclass(frozen=True, slots=True)
class ResearchPipelineResult:
    candidates: tuple[ResearchCandidate, ...]
    rejections: tuple[Rejection, ...]
    orders_authorized: bool = False


def analyze_research_universe(
    *,
    contracts: Iterable[Contract],
    market_snapshots: Iterable[ScannerMarketSnapshot],
    candles_by_symbol: Mapping[str, Iterable[Candle]],
    structure_inputs: Iterable[MarketStructureInput],
    now: datetime,
    scanner_config: ScannerConfig | None = None,
    opportunity_config: OpportunityConfig | None = None,
    research_config: ResearchConfig | None = None,
    liquidity_config: LiquidityConfig | None = None,
) -> ResearchPipelineResult:
    """Run the upstream engines and return review-only candidates.

    A candidate is REVIEW only when the opportunity engine, strategy research and
    liquidity/structure engine all agree on the same direction. Missing evidence,
    conflicts, or invalid/stale input produce NO_TRADE. This is not a signal and
    cannot be passed to execution without a separately created and validated signal.
    """
    scan: ScanResult = scan_candidates(
        contracts, market_snapshots, now=now, config=scanner_config
    )
    liquidity_rows = rank_opportunities(
        structure_inputs, now=now, config=liquidity_config
    )
    liquidity_by_symbol = {row.symbol.upper(): row for row in liquidity_rows}
    candles_index = {symbol.strip().upper(): tuple(rows)
                     for symbol, rows in candles_by_symbol.items()}

    results: list[ResearchCandidate] = []
    for scanned in scan.candidates:
        symbol = scanned.symbol.strip().upper()
        candles = candles_index.get(symbol)
        if not candles:
            results.append(_no_trade(
                scanned, "candle history is missing; downstream analysis was not run"
            ))
            continue

        try:
            opportunity = assess_opportunity(
                symbol, candles, now=now, config=opportunity_config,
                research_config=research_config,
            )
            research = research_strategies(
                symbol, candles, now=now, config=research_config
            )
        except (ValueError, TypeError) as exc:
            results.append(_no_trade(
                scanned, f"downstream research input invalid: {type(exc).__name__}"
            ))
            continue

        liquidity = liquidity_by_symbol.get(symbol)
        expected_direction = (
            "LONG" if research.decision == "REVIEW_LONG" else
            "SHORT" if research.decision == "REVIEW_SHORT" else "NONE"
        )
        reasons: list[str] = []
        if opportunity.status != "REVIEW" or opportunity.direction == "NONE":
            reasons.append("opportunity engine did not approve research review")
        if expected_direction == "NONE":
            reasons.append("strategy research returned NO_TRADE")
        if liquidity is None:
            reasons.append("liquidity/structure evidence is missing or invalid")
        elif liquidity.status != "REVIEW" or liquidity.direction != expected_direction:
            reasons.append("liquidity/structure direction does not confirm strategy research")
        if opportunity.direction != expected_direction:
            reasons.append("opportunity and strategy directions disagree")

        aligned = (
            opportunity.status == "REVIEW"
            and opportunity.direction in ("LONG", "SHORT")
            and expected_direction == opportunity.direction
            and liquidity is not None
            and liquidity.status == "REVIEW"
            and liquidity.direction == expected_direction
        )
        if aligned:
            status = "REVIEW"
            direction = expected_direction
            reasons.extend((
                "scanner accepted market for research",
                "opportunity, strategy and liquidity engines agree",
                "research-only: explicit signal prices and independent validation are still required",
            ))
        else:
            status = "NO_TRADE"
            direction = "NONE"
            if not reasons:
                reasons.append("insufficient independent engine agreement")

        results.append(ResearchCandidate(
            symbol=symbol,
            scanner_score=scanned.score,
            status=status,
            direction=direction,
            reasons=tuple(reasons),
            opportunity=opportunity,
            strategy_research=research,
            liquidity=liquidity,
            orders_authorized=False,
        ))

    return ResearchPipelineResult(
        candidates=tuple(results),
        rejections=scan.rejections,
        orders_authorized=False,
    )


def _no_trade(candidate: ScanCandidate, reason: str) -> ResearchCandidate:
    return ResearchCandidate(
        symbol=candidate.symbol.strip().upper(),
        scanner_score=candidate.score,
        status="NO_TRADE",
        direction="NONE",
        reasons=(reason,),
        opportunity=None,
        strategy_research=None,
        liquidity=None,
        orders_authorized=False,
    )
