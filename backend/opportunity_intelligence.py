"""Deeper, research-only market opportunity assessment and agent notifications.

Notifications are returned as durable-message-shaped records for a future message
bus. This module has no network, exchange, account, or order-execution capability.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import math
from statistics import fmean, pstdev
from typing import Iterable

from backend.strategy_research import Candle, ResearchConfig, research_strategies


@dataclass(frozen=True, slots=True)
class OpportunityConfig:
    min_history: int = 30
    high_volatility_pct: float = 2.5
    minimum_opportunity_score: float = 55.0
    max_signal_age_seconds: int = 180
    recipients: tuple[str, ...] = (
        "strategy_coordinator",
        "risk_guardian",
        "portfolio_coordinator",
        "decision_journal",
    )

    def __post_init__(self) -> None:
        if self.min_history < 3:
            raise ValueError("min_history must be at least 3")
        if not math.isfinite(self.high_volatility_pct) or self.high_volatility_pct <= 0:
            raise ValueError("high_volatility_pct must be finite and positive")
        if not math.isfinite(self.minimum_opportunity_score) or not 0 <= self.minimum_opportunity_score <= 100:
            raise ValueError("minimum_opportunity_score must be between 0 and 100")
        if self.max_signal_age_seconds <= 0:
            raise ValueError("max_signal_age_seconds must be positive")
        if not self.recipients or any(not recipient.strip() for recipient in self.recipients):
            raise ValueError("recipients must contain non-empty agent names")


@dataclass(frozen=True, slots=True)
class AgentNotification:
    event_type: str
    recipient: str
    opportunity_id: str
    symbol: str
    status: str
    direction: str
    score: float
    summary: str
    orders_authorized: bool = False


@dataclass(frozen=True, slots=True)
class OpportunityReport:
    opportunity_id: str
    symbol: str
    assessed_at: datetime
    status: str
    direction: str
    market_regime: str
    opportunity_score: float
    strategy_consensus: str
    evidence: tuple[str, ...]
    risks: tuple[str, ...]
    notifications: tuple[AgentNotification, ...]
    orders_authorized: bool = False


def _empty_report(symbol: str, now: datetime, reason: str, recipients: tuple[str, ...]) -> OpportunityReport:
    token = f"{symbol}|{now.astimezone(timezone.utc).isoformat()}|NO_TRADE"
    opportunity_id = hashlib.sha256(token.encode()).hexdigest()[:20]
    notifications = tuple(AgentNotification(
        "opportunity.assessed", recipient, opportunity_id, symbol, "NO_TRADE", "NONE", 0.0,
        reason, False,
    ) for recipient in recipients)
    return OpportunityReport(opportunity_id, symbol, now, "NO_TRADE", "NONE", "UNKNOWN", 0.0,
                             "NO_CONSENSUS", (reason,), ("Research is incomplete or invalid",),
                             notifications, False)


def assess_opportunity(
    symbol: str,
    candles: Iterable[Candle],
    *,
    now: datetime,
    config: OpportunityConfig | None = None,
    research_config: ResearchConfig | None = None,
) -> OpportunityReport:
    """Assess context, strategy agreement, and risks; return notifications, never orders."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(timezone.utc)
    config = config or OpportunityConfig()
    rows = list(candles)
    if not symbol.strip():
        return _empty_report(symbol, now, "symbol is missing", config.recipients)
    if len(rows) < config.min_history:
        return _empty_report(symbol, now, f"insufficient history: need {config.min_history} candles",
                             config.recipients)

    if rows and rows[-1].timestamp.tzinfo is not None and rows[-1].timestamp.utcoffset() is not None:
        latest_age = (now - rows[-1].timestamp.astimezone(timezone.utc)).total_seconds()
        if latest_age > config.max_signal_age_seconds:
            return _empty_report(symbol, now, "latest candle exceeds opportunity freshness limit",
                                 config.recipients)

    research = research_strategies(symbol, rows, now=now, config=research_config)
    if research.decision == "NO_TRADE" and research.reasons and any(
        phrase in research.reasons[0].lower() for phrase in
        ("stale", "future", "invalid", "missing", "mixed", "timezone", "inconsistent", "strictly increasing", "insufficient")
    ):
        return _empty_report(symbol, now, research.reasons[0], config.recipients)

    closes = [row.close for row in rows]
    returns = [(closes[i] / closes[i - 1] - 1.0) * 100.0 for i in range(1, len(closes))]
    recent_returns = returns[-min(20, len(returns)):]
    volatility_pct = pstdev(recent_returns) if len(recent_returns) > 1 else 0.0
    fast_mean = fmean(closes[-5:])
    slow_mean = fmean(closes[-20:])
    drift_pct = (fast_mean / slow_mean - 1.0) * 100.0 if slow_mean else 0.0

    if volatility_pct >= config.high_volatility_pct:
        regime = "HIGH_VOLATILITY"
    elif abs(drift_pct) >= 0.35:
        regime = "TRENDING_UP" if drift_pct > 0 else "TRENDING_DOWN"
    else:
        regime = "RANGE_OR_UNCLEAR"

    directional = [item for item in research.assessments if item.direction in ("LONG", "SHORT")]
    strong = [item for item in directional if item.confidence_score >= 60.0]
    long_count = sum(item.direction == "LONG" for item in strong)
    short_count = sum(item.direction == "SHORT" for item in strong)
    consensus = "LONG" if long_count >= 2 and long_count > short_count else (
        "SHORT" if short_count >= 2 and short_count > long_count else "NO_CONSENSUS")
    if research.decision == "REVIEW_LONG" and consensus == "LONG":
        direction = "LONG"
    elif research.decision == "REVIEW_SHORT" and consensus == "SHORT":
        direction = "SHORT"
    else:
        direction = "NONE"

    average_strength = fmean(item.confidence_score for item in strong) if strong else 0.0
    agreement_ratio = max(long_count, short_count) / max(1, len(strong))
    regime_alignment = 10.0 if ((direction == "LONG" and regime == "TRENDING_UP") or
                                (direction == "SHORT" and regime == "TRENDING_DOWN")) else 0.0
    score = min(100.0, max(0.0, average_strength * 0.65 + agreement_ratio * 25.0 + regime_alignment))

    risks: list[str] = []
    if regime == "HIGH_VOLATILITY":
        risks.append("high short-term return volatility; require stricter risk review")
    if direction != "NONE" and regime in ("TRENDING_UP", "TRENDING_DOWN") and (
        (direction == "LONG" and regime == "TRENDING_DOWN") or
        (direction == "SHORT" and regime == "TRENDING_UP")
    ):
        risks.append("strategy direction conflicts with broad short-term drift")
    if len(strong) < 2:
        risks.append("fewer than two strong strategy assessments")
    if not risks:
        risks.append("market and execution risks still require independent assessment")

    status = "REVIEW" if direction != "NONE" and score >= config.minimum_opportunity_score else "NO_TRADE"
    if status == "NO_TRADE":
        direction = "NONE"
    evidence = (
        f"market regime: {regime}",
        f"20-interval return volatility (std dev): {volatility_pct:.4f}%",
        f"short-vs-long mean drift: {drift_pct:.4f}%",
        f"strong strategy votes: long={long_count}, short={short_count}",
        f"research engine decision: {research.decision}",
        "score is a screening heuristic, not a calibrated probability or profit forecast",
    )
    token = f"{symbol}|{rows[-1].timestamp.astimezone(timezone.utc).isoformat()}|{direction}|{status}"
    opportunity_id = hashlib.sha256(token.encode()).hexdigest()[:20]
    summary = f"{status}: {direction}; regime={regime}; score={score:.1f}; research-only"
    notifications = tuple(AgentNotification(
        "opportunity.assessed", recipient, opportunity_id, symbol, status, direction,
        round(score, 2), summary, False,
    ) for recipient in config.recipients)
    return OpportunityReport(opportunity_id, symbol, now, status, direction, regime, round(score, 2),
                             consensus, evidence, tuple(risks), notifications, False)
