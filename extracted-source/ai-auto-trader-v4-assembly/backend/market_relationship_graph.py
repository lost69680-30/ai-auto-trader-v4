"""Deterministic, research-only market relationship graph preparation.

This module builds an undirected correlation graph from aligned, closed-period
return observations. It is a GAT input-preparation layer, not a trained GAT,
signal generator, trading policy, or risk authority.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import hashlib
import json
import math


@dataclass(frozen=True, slots=True)
class MarketRelationshipNode:
    symbol: str
    observation_count: int
    mean_return: float
    volatility: float
    degree: int
    degree_centrality: float


@dataclass(frozen=True, slots=True)
class MarketRelationshipEdge:
    source: str
    target: str
    correlation: float
    relationship: str


@dataclass(frozen=True, slots=True)
class MarketRelationshipGraph:
    input_fingerprint: str
    correlation_threshold: float
    nodes: tuple[MarketRelationshipNode, ...]
    edges: tuple[MarketRelationshipEdge, ...]
    orders_authorized: bool = False


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


def _canonical_symbol(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value != value.upper()
        or not value.isalnum()
    ):
        raise ValueError("graph symbols must be canonical uppercase alphanumeric values")
    return value


def build_market_relationship_graph(
    return_series: Mapping[str, Iterable[int | float]],
    *,
    correlation_threshold: float = 0.70,
    min_observations: int = 30,
) -> MarketRelationshipGraph:
    """Build a bounded pairwise correlation graph from aligned return arrays.

    Inputs must represent the same consecutive, closed-candle observation window
    for every symbol. Missing/gapped observations must be repaired upstream;
    this function rejects different series lengths and non-finite values.
    Correlation is Pearson correlation over the supplied returns. Zero-variance
    series are retained as isolated nodes rather than assigned fabricated edges.
    """
    threshold = _finite_number(correlation_threshold, "correlation_threshold")
    if not 0.0 < threshold <= 1.0:
        raise ValueError("correlation_threshold must be in (0, 1]")
    if isinstance(min_observations, bool) or not isinstance(min_observations, int) or min_observations < 3:
        raise ValueError("min_observations must be an integer of at least 3")
    if not isinstance(return_series, Mapping) or len(return_series) < 2:
        raise ValueError("return_series must contain at least two symbols")
    if len(return_series) > 100:
        raise ValueError("relationship graph is limited to 100 symbols per snapshot")

    normalized: dict[str, tuple[float, ...]] = {}
    for raw_symbol, raw_values in return_series.items():
        symbol = _canonical_symbol(raw_symbol)
        if symbol in normalized:
            raise ValueError("duplicate graph symbols are not allowed")
        if isinstance(raw_values, (str, bytes)):
            raise ValueError(f"return series for {symbol} must be a numeric iterable")
        try:
            values = tuple(_finite_number(value, f"{symbol} return") for value in raw_values)
        except TypeError as exc:
            raise ValueError(f"return series for {symbol} must be iterable") from exc
        if len(values) < min_observations:
            raise ValueError(f"{symbol} has fewer than the minimum observation count")
        normalized[symbol] = values

    lengths = {len(values) for values in normalized.values()}
    if len(lengths) != 1:
        raise ValueError("all graph return series must have the same number of observations")
    observation_count = next(iter(lengths))
    symbols = tuple(sorted(normalized))
    means = {
        symbol: math.fsum(normalized[symbol]) / observation_count
        for symbol in symbols
    }
    centered = {
        symbol: tuple(value - means[symbol] for value in normalized[symbol])
        for symbol in symbols
    }
    sums_of_squares = {
        symbol: math.fsum(value * value for value in centered[symbol])
        for symbol in symbols
    }

    edges: list[MarketRelationshipEdge] = []
    degrees = {symbol: 0 for symbol in symbols}
    for left_index, source in enumerate(symbols):
        for target in symbols[left_index + 1:]:
            left_ss = sums_of_squares[source]
            right_ss = sums_of_squares[target]
            if left_ss <= 0.0 or right_ss <= 0.0:
                continue
            covariance = math.fsum(
                left_value * right_value
                for left_value, right_value in zip(centered[source], centered[target])
            )
            correlation = covariance / math.sqrt(left_ss * right_ss)
            correlation = max(-1.0, min(1.0, correlation))
            if abs(correlation) < threshold:
                continue
            edges.append(MarketRelationshipEdge(
                source=source,
                target=target,
                correlation=correlation,
                relationship="POSITIVE" if correlation >= 0.0 else "INVERSE",
            ))
            degrees[source] += 1
            degrees[target] += 1

    denominator = max(1, len(symbols) - 1)
    nodes = tuple(
        MarketRelationshipNode(
            symbol=symbol,
            observation_count=observation_count,
            mean_return=means[symbol],
            volatility=math.sqrt(sums_of_squares[symbol] / observation_count),
            degree=degrees[symbol],
            degree_centrality=degrees[symbol] / denominator,
        )
        for symbol in symbols
    )
    material = json.dumps(
        {symbol: list(normalized[symbol]) for symbol in symbols},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    fingerprint = hashlib.sha256(material).hexdigest()
    return MarketRelationshipGraph(
        input_fingerprint=fingerprint,
        correlation_threshold=threshold,
        nodes=nodes,
        edges=tuple(edges),
        orders_authorized=False,
    )
