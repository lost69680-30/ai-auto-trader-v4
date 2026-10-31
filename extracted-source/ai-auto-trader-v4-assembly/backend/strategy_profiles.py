"""Versioned Solo strategy profiles backed by the existing adaptive requirements."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from backend.adaptive_strategy import _EXECUTABLE_SPECS, _LONG_TERM_TIMEFRAMES


@dataclass(frozen=True, slots=True)
class StrategyProfile:
    strategy_id: str
    adaptive_strategy_name: str
    required_timeframes: tuple[str, ...]
    strategy_version: str = "1.0.0"
    analysis_only: bool = False

    def __post_init__(self) -> None:
        if self.strategy_id not in {"scalping", "day", "swing", "position", "long_term"}:
            raise ValueError("unsupported strategy_id")
        if not isinstance(self.adaptive_strategy_name, str) or not self.adaptive_strategy_name.strip():
            raise ValueError("adaptive_strategy_name is required")
        if not self.required_timeframes or any(
            not isinstance(item, str) or not item.strip() for item in self.required_timeframes
        ):
            raise ValueError("required_timeframes must contain non-empty timeframe names")
        if not isinstance(self.strategy_version, str) or not self.strategy_version.strip():
            raise ValueError("strategy_version is required")
        if not isinstance(self.analysis_only, bool):
            raise ValueError("analysis_only must be boolean")
        if (self.strategy_id == "long_term") != self.analysis_only:
            raise ValueError("long_term must be analysis-only and executable strategies must not")


def _profile(
    strategy_id: str,
    adaptive_name: str,
    timeframes: Iterable[str],
    *,
    analysis_only: bool = False,
) -> StrategyProfile:
    return StrategyProfile(
        strategy_id=strategy_id,
        adaptive_strategy_name=adaptive_name,
        required_timeframes=tuple(timeframes),
        strategy_version="1.0.0",
        analysis_only=analysis_only,
    )


DEFAULT_STRATEGY_PROFILES = (
    _profile("scalping", "SCALPING", _EXECUTABLE_SPECS["SCALPING"]["timeframes"]),
    _profile("day", "DAY_TRADING", _EXECUTABLE_SPECS["DAY_TRADING"]["timeframes"]),
    _profile("swing", "SWING_TRADING", _EXECUTABLE_SPECS["SWING_TRADING"]["timeframes"]),
    _profile("position", "POSITION_TRADING", _EXECUTABLE_SPECS["POSITION_TRADING"]["timeframes"]),
    _profile("long_term", "LONG_TERM_ANALYSIS", _LONG_TERM_TIMEFRAMES, analysis_only=True),
)
