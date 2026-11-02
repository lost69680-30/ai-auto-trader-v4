"""Research-only pairwise portfolio correlation diagnostics.

This module reports descriptive co-movement from aligned, timestamped returns.
It does not size positions, alter Risk Guardian limits, or authorize orders.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class PairwiseCorrelation:
    symbol_a: str
    symbol_b: str
    observations: int
    correlation: float | None
    status: str


@dataclass(frozen=True, slots=True)
class PortfolioCorrelationReport:
    min_observations: int
    pairs: tuple[PairwiseCorrelation, ...]
    orders_authorized: bool = False


@dataclass(frozen=True, slots=True)
class PortfolioCorrelationSummary:
    pair_count: int
    defined_pair_count: int
    undefined_pair_count: int
    coverage_pct: float | None
    max_abs_correlation: float | None
    high_correlation_pairs: tuple[PairwiseCorrelation, ...]
    high_correlation_threshold: float
    orders_authorized: bool = False


def _valid_symbol(symbol: object) -> bool:
    return (
        isinstance(symbol, str)
        and bool(symbol)
        and symbol == symbol.strip().upper()
        and all(char.isalnum() for char in symbol)
    )


def _finite_return(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("returns must contain finite numeric values")
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("returns must contain finite numeric values") from exc
    if not math.isfinite(result):
        raise ValueError("returns must contain finite numeric values")
    return result


def calculate_pairwise_correlations(
    returns_by_symbol: Mapping[str, Mapping[int, float]],
    *,
    min_observations: int = 30,
) -> PortfolioCorrelationReport:
    """Calculate Pearson correlations on exact shared timestamps only.

    Missing timestamps are never forward-filled or interpolated. A pair with
    too few shared observations or zero variance has an undefined correlation
    (None), not a fabricated zero. All supplied observations are validated;
    malformed data rejects the complete report rather than being silently
    discarded.
    """
    if not isinstance(returns_by_symbol, Mapping):
        raise ValueError("returns_by_symbol must be a mapping")
    if (
        isinstance(min_observations, bool)
        or not isinstance(min_observations, int)
        or min_observations < 2
    ):
        raise ValueError("min_observations must be an integer of at least 2")

    normalized: dict[str, dict[int, float]] = {}
    for symbol, observations in returns_by_symbol.items():
        if not _valid_symbol(symbol):
            raise ValueError("each asset symbol must be canonical uppercase text")
        if not isinstance(observations, Mapping):
            raise ValueError("each asset return series must be a timestamp mapping")
        clean: dict[int, float] = {}
        for timestamp, value in observations.items():
            if (
                isinstance(timestamp, bool)
                or not isinstance(timestamp, int)
                or timestamp < 0
            ):
                raise ValueError("return timestamps must be non-negative integers")
            clean[timestamp] = _finite_return(value)
        normalized[symbol] = clean

    symbols = sorted(normalized)
    pairs: list[PairwiseCorrelation] = []
    for index, symbol_a in enumerate(symbols):
        for symbol_b in symbols[index + 1:]:
            shared = sorted(set(normalized[symbol_a]).intersection(normalized[symbol_b]))
            count = len(shared)
            if count < min_observations:
                pairs.append(PairwiseCorrelation(
                    symbol_a, symbol_b, count, None, "INSUFFICIENT_OVERLAP"
                ))
                continue

            values_a = [normalized[symbol_a][stamp] for stamp in shared]
            values_b = [normalized[symbol_b][stamp] for stamp in shared]

            # Scale each series before centering. Pearson correlation is
            # invariant to positive scaling, while this keeps intermediate
            # squares/products bounded for extreme but finite input values.
            scale_a = max(abs(value) for value in values_a)
            scale_b = max(abs(value) for value in values_b)
            if scale_a == 0 or scale_b == 0:
                pairs.append(PairwiseCorrelation(
                    symbol_a, symbol_b, count, None, "ZERO_VARIANCE"
                ))
                continue
            scaled_a = [value / scale_a for value in values_a]
            scaled_b = [value / scale_b for value in values_b]
            mean_a = math.fsum(scaled_a) / count
            mean_b = math.fsum(scaled_b) / count
            centered_a = [value - mean_a for value in scaled_a]
            centered_b = [value - mean_b for value in scaled_b]
            variance_a = math.fsum(value * value for value in centered_a)
            variance_b = math.fsum(value * value for value in centered_b)
            if variance_a <= 0 or variance_b <= 0:
                pairs.append(PairwiseCorrelation(
                    symbol_a, symbol_b, count, None, "ZERO_VARIANCE"
                ))
                continue
            covariance = math.fsum(
                left * right for left, right in zip(centered_a, centered_b)
            )
            correlation = covariance / math.sqrt(variance_a * variance_b)
            if not math.isfinite(correlation):
                raise ValueError("computed correlation is not finite")
            correlation = max(-1.0, min(1.0, correlation))
            pairs.append(PairwiseCorrelation(
                symbol_a, symbol_b, count, round(correlation, 8), "OK"
            ))

    return PortfolioCorrelationReport(
        min_observations=min_observations,
        pairs=tuple(pairs),
        orders_authorized=False,
    )


def summarize_portfolio_correlations(
    report: PortfolioCorrelationReport,
    *,
    high_correlation_threshold: float,
) -> PortfolioCorrelationSummary:
    """Summarize observed correlation coverage; undefined pairs stay undefined."""
    if not isinstance(report, PortfolioCorrelationReport) or report.orders_authorized is not False:
        raise ValueError("a paper-only PortfolioCorrelationReport is required")
    if (
        isinstance(high_correlation_threshold, bool)
        or not isinstance(high_correlation_threshold, (int, float))
    ):
        raise ValueError("high_correlation_threshold must be finite and between 0 and 1")
    try:
        threshold = float(high_correlation_threshold)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("high_correlation_threshold must be finite and between 0 and 1") from exc
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("high_correlation_threshold must be finite and between 0 and 1")
    defined = tuple(
        pair for pair in report.pairs
        if pair.status == "OK" and pair.correlation is not None
    )
    undefined_count = len(report.pairs) - len(defined)
    pair_count = len(report.pairs)
    coverage = (len(defined) / pair_count * 100.0) if pair_count else None
    max_abs = max((abs(pair.correlation) for pair in defined), default=None)
    high_pairs = tuple(
        pair for pair in defined
        if abs(pair.correlation) >= threshold
    )
    return PortfolioCorrelationSummary(
        pair_count=pair_count,
        defined_pair_count=len(defined),
        undefined_pair_count=undefined_count,
        coverage_pct=round(coverage, 4) if coverage is not None else None,
        max_abs_correlation=round(max_abs, 8) if max_abs is not None else None,
        high_correlation_pairs=high_pairs,
        high_correlation_threshold=threshold,
        orders_authorized=False,
    )
