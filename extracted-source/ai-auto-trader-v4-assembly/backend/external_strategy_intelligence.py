"""Provenance-first intake for external strategy ideas.

Public leaderboard statistics are preserved as source evidence only. This
module never infers missing rules, treats published metrics as calibrated
probabilities, activates a strategy, or authorizes orders.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from collections.abc import Mapping
from urllib.parse import urlsplit
from typing import Any


_SUPPORTED_RULE_FAMILIES = {
    "SMA_CROSS",
    "MOMENTUM",
    "BREAKOUT",
    "RSI_THRESHOLD",
}


def _text(value: object, field_name: str, maximum: int = 240) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum:
        raise ValueError(f"{field_name} must be a trimmed non-empty string")
    return value


def _aware(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _finite_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be a finite number")
    return number


@dataclass(frozen=True, slots=True)
class ExternalPerformanceEvidence:
    sample_size: int
    backtest_start: datetime
    backtest_end: datetime
    published_metrics: Mapping[str, int | float]
    methodology: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.sample_size, bool) or not isinstance(self.sample_size, int) or self.sample_size < 0:
            raise ValueError("sample_size must be a non-negative integer")
        start = _aware(self.backtest_start, "backtest_start")
        end = _aware(self.backtest_end, "backtest_end")
        if start >= end:
            raise ValueError("backtest window must have start before end")
        if not isinstance(self.published_metrics, Mapping):
            raise ValueError("published_metrics must be a mapping")
        for key, value in self.published_metrics.items():
            _text(key, "published metric name", 80)
            _finite_number(value, f"published metric {key}")
        if self.methodology is not None and not isinstance(self.methodology, str):
            raise ValueError("methodology must be text or None")


@dataclass(frozen=True, slots=True)
class ExternalRuleSpec:
    family: str
    parameters: Mapping[str, int | float]
    exit_policy: str

    def __post_init__(self) -> None:
        family = _text(self.family, "rule family", 40)
        if family not in _SUPPORTED_RULE_FAMILIES:
            raise ValueError("unsupported external strategy rule family")
        if not isinstance(self.parameters, Mapping):
            raise ValueError("rule parameters must be a mapping")
        exit_policy = _text(self.exit_policy, "exit policy", 40)
        # The current simulator implements only an explicit one-bar exit.
        # Unsupported published exit rules must not be silently approximated.
        if exit_policy != "ONE_BAR":
            raise ValueError("unsupported external strategy exit policy")
        for key, value in self.parameters.items():
            _text(key, "rule parameter name", 80)
            _finite_number(value, f"rule parameter {key}")
        required = {
            "SMA_CROSS": {"fast_period", "slow_period"},
            "MOMENTUM": {"lookback", "threshold_pct"},
            "BREAKOUT": {"lookback", "buffer_pct"},
            "RSI_THRESHOLD": {"period", "oversold", "overbought"},
        }[family]
        if set(self.parameters) != required:
            raise ValueError("rule parameters do not match the supported family contract")
        integer_fields = {
            "SMA_CROSS": ("fast_period", "slow_period"),
            "MOMENTUM": ("lookback",),
            "BREAKOUT": ("lookback",),
            "RSI_THRESHOLD": ("period",),
        }[family]
        for name in integer_fields:
            value = self.parameters[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 2:
                raise ValueError(f"{name} must be an integer of at least 2")
        if family == "SMA_CROSS" and self.parameters["fast_period"] >= self.parameters["slow_period"]:
            raise ValueError("SMA fast_period must be smaller than slow_period")
        if family == "MOMENTUM" and self.parameters["threshold_pct"] <= 0:
            raise ValueError("momentum threshold_pct must be positive")
        if family == "BREAKOUT" and self.parameters["buffer_pct"] < 0:
            raise ValueError("breakout buffer_pct must be non-negative")
        if family == "RSI_THRESHOLD":
            oversold = self.parameters["oversold"]
            overbought = self.parameters["overbought"]
            if not 0 < oversold < overbought < 100:
                raise ValueError("RSI thresholds must satisfy 0 < oversold < overbought < 100")


@dataclass(frozen=True, slots=True)
class ExternalStrategyRecord:
    source_name: str
    source_url: str
    source_record_id: str
    title: str
    captured_at: datetime
    published_at: datetime | None
    market_types: tuple[str, ...]
    symbols: tuple[str, ...]
    timeframes: tuple[str, ...]
    indicators: tuple[str, ...]
    performance: ExternalPerformanceEvidence
    rules: ExternalRuleSpec | None = None

    def __post_init__(self) -> None:
        _text(self.source_name, "source name", 120)
        url = _text(self.source_url, "source URL", 1000)
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ValueError("source URL must be an absolute HTTP(S) URL")
        _text(self.source_record_id, "source record id", 160)
        _text(self.title, "strategy title", 240)
        _aware(self.captured_at, "captured_at")
        if self.published_at is not None:
            _aware(self.published_at, "published_at")
        for name, values in (
            ("market_types", self.market_types),
            ("symbols", self.symbols),
            ("timeframes", self.timeframes),
            ("indicators", self.indicators),
        ):
            if not isinstance(values, tuple):
                raise ValueError(f"{name} must be a tuple")
            for value in values:
                _text(value, name, 80)
        if not isinstance(self.performance, ExternalPerformanceEvidence):
            raise ValueError("performance evidence is required")
        if self.rules is not None and not isinstance(self.rules, ExternalRuleSpec):
            raise ValueError("rules must be an explicit ExternalRuleSpec or None")


@dataclass(frozen=True, slots=True)
class ExternalStrategyCandidate:
    candidate_id: str
    candidate_fingerprint: str
    source_provenance_id: str
    title: str
    rule_spec: ExternalRuleSpec
    market_types: tuple[str, ...]
    symbols: tuple[str, ...]
    timeframes: tuple[str, ...]
    orders_authorized: bool = False


@dataclass(frozen=True, slots=True)
class ExternalStrategyAssessment:
    status: str
    source_name: str
    provenance_id: str
    sample_size: int
    published_metrics: Mapping[str, int | float]
    limitations: tuple[str, ...]
    calibration_status: str = "NOT_CALIBRATED"
    candidate: ExternalStrategyCandidate | None = None
    orders_authorized: bool = False


@dataclass(frozen=True, slots=True)
class ExternalStrategyAssessmentConfig:
    minimum_sample_size: int = 30

    def __post_init__(self) -> None:
        if (
            isinstance(self.minimum_sample_size, bool)
            or not isinstance(self.minimum_sample_size, int)
            or self.minimum_sample_size < 1
        ):
            raise ValueError("minimum_sample_size must be a positive integer")


def assess_external_strategy(
    record: ExternalStrategyRecord,
    *,
    config: ExternalStrategyAssessmentConfig | None = None,
) -> ExternalStrategyAssessment:
    """Preserve provenance and decide whether an idea is reproducible enough to test.

    RESEARCH_CANDIDATE means only that explicit rules and source evidence are
    sufficient to create a reproducible internal candidate. It is not a
    profitability finding, promotion, strategy activation, or risk approval.
    """
    if not isinstance(record, ExternalStrategyRecord):
        raise ValueError("record must be an ExternalStrategyRecord")
    cfg = config or ExternalStrategyAssessmentConfig()
    captured = _aware(record.captured_at, "captured_at")
    provenance_material = "|".join((
        record.source_name,
        record.source_url,
        record.source_record_id,
        captured.isoformat(),
    ))
    provenance_id = hashlib.sha256(provenance_material.encode("utf-8")).hexdigest()
    limitations = [
        "External published performance is unverified source evidence, not a forecast.",
        "Published metrics are not calibrated probabilities or expected returns.",
        "No strategy is copied, activated, promoted, or connected to execution.",
    ]

    status = "RESEARCH_CANDIDATE"
    candidate = None
    evidence = record.performance
    if evidence.sample_size < cfg.minimum_sample_size:
        status = "INSUFFICIENT_SAMPLE"
        limitations.append(
            f"Sample size {evidence.sample_size} is below the configured minimum {cfg.minimum_sample_size}."
        )
    elif record.rules is None:
        status = "IDEA_ONLY"
        limitations.append("Published strategy rules are unavailable; proprietary rules were not inferred.")
    elif evidence.methodology is None or not evidence.methodology.strip():
        status = "METHODOLOGY_MISSING"
        limitations.append("Backtest methodology is missing; reproducibility cannot be established.")
    else:
        rule_payload = {
            "family": record.rules.family,
            "parameters": dict(record.rules.parameters),
            "exit_policy": record.rules.exit_policy,
        }
        identity = {
            "provenance_id": provenance_id,
            "rule_spec": rule_payload,
            "market_types": list(record.market_types),
            "symbols": list(record.symbols),
            "timeframes": list(record.timeframes),
        }
        fingerprint = hashlib.sha256(json.dumps(
            identity, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")).hexdigest()
        candidate = ExternalStrategyCandidate(
            candidate_id="external-" + fingerprint[:32],
            candidate_fingerprint=fingerprint,
            source_provenance_id=provenance_id,
            title=record.title,
            rule_spec=record.rules,
            market_types=record.market_types,
            symbols=record.symbols,
            timeframes=record.timeframes,
            orders_authorized=False,
        )

    return ExternalStrategyAssessment(
        status=status,
        source_name=record.source_name,
        provenance_id=provenance_id,
        sample_size=evidence.sample_size,
        published_metrics=dict(evidence.published_metrics),
        limitations=tuple(limitations),
        calibration_status="NOT_CALIBRATED",
        candidate=candidate,
        orders_authorized=False,
    )


def external_rule_minimum_candles(rule_spec: ExternalRuleSpec) -> int:
    if not isinstance(rule_spec, ExternalRuleSpec):
        raise ValueError("an explicit supported ExternalRuleSpec is required")
    params = rule_spec.parameters
    if rule_spec.family == "SMA_CROSS":
        return int(params["slow_period"])
    if rule_spec.family in {"MOMENTUM", "BREAKOUT"}:
        return int(params["lookback"]) + 1
    return int(params["period"]) + 1


def evaluate_external_rule(rule_spec: ExternalRuleSpec, candles: Any) -> str:
    """Evaluate only the explicit, bounded rule DSL; return LONG, SHORT or NEUTRAL."""
    if not isinstance(rule_spec, ExternalRuleSpec):
        raise ValueError("an explicit supported ExternalRuleSpec is required")
    rows = list(candles)
    if len(rows) < external_rule_minimum_candles(rule_spec):
        return "NEUTRAL"
    closes = [float(row.close) for row in rows]
    last = closes[-1]
    params = rule_spec.parameters

    if rule_spec.family == "SMA_CROSS":
        fast_period = int(params["fast_period"])
        slow_period = int(params["slow_period"])
        fast = math.fsum(closes[-fast_period:]) / fast_period
        slow = math.fsum(closes[-slow_period:]) / slow_period
        return "LONG" if fast > slow else "SHORT" if fast < slow else "NEUTRAL"

    if rule_spec.family == "MOMENTUM":
        lookback = int(params["lookback"])
        change_pct = (last / closes[-lookback - 1] - 1.0) * 100.0
        threshold = float(params["threshold_pct"])
        return "LONG" if change_pct >= threshold else (
            "SHORT" if change_pct <= -threshold else "NEUTRAL"
        )

    if rule_spec.family == "BREAKOUT":
        lookback = int(params["lookback"])
        prior = rows[-lookback - 1:-1]
        prior_high = max(float(row.high) for row in prior)
        prior_low = min(float(row.low) for row in prior)
        buffer = float(params["buffer_pct"]) / 100.0
        return "LONG" if last > prior_high * (1.0 + buffer) else (
            "SHORT" if last < prior_low * (1.0 - buffer) else "NEUTRAL"
        )

    period = int(params["period"])
    changes = [closes[index] - closes[index - 1] for index in range(len(closes) - period, len(closes))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    average_gain = math.fsum(gains) / period
    average_loss = math.fsum(losses) / period
    if average_loss == 0:
        rsi = 50.0 if average_gain == 0 else 100.0
    elif average_gain == 0:
        rsi = 0.0
    else:
        relative_strength = average_gain / average_loss
        rsi = 100.0 - 100.0 / (1.0 + relative_strength)
    return "LONG" if rsi <= float(params["oversold"]) else (
        "SHORT" if rsi >= float(params["overbought"]) else "NEUTRAL"
    )


def evaluate_external_candidate(
    candidate: ExternalStrategyCandidate,
    candles: Any,
    *,
    symbol: str,
    walk_forward_config: Any,
    backtest_config: Any = None,
    research_config: Any = None,
    research_now: datetime,
):
    """Run explicit external rules through the offline, cost-aware OOS harness."""
    if not isinstance(candidate, ExternalStrategyCandidate) or candidate.orders_authorized is not False:
        raise ValueError("a paper-only external strategy candidate is required")
    if not isinstance(candidate.rule_spec, ExternalRuleSpec):
        raise ValueError("candidate does not contain explicit reproducible rules")
    symbol = _text(symbol, "evaluation symbol", 30)
    if candidate.symbols and symbol not in candidate.symbols:
        raise ValueError("evaluation symbol is outside the candidate's published market scope")
    from backend.walk_forward import evaluate_walk_forward

    return evaluate_walk_forward(
        symbol,
        candles,
        config=walk_forward_config,
        backtest_config=backtest_config,
        research_config=research_config,
        research_now=research_now,
        rule_spec=candidate.rule_spec,
    )
