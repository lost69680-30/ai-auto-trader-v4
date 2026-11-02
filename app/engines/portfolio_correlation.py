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
            mean_a = math.fsum(values_a) / count
            mean_b = math.fsum(values_b) / count
            centered_a = [value - mean_a for value in values_a]
            centered_b = [value - mean_b for value in values_b]
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
