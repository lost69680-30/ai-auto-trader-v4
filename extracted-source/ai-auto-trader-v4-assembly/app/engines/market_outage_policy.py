"""Explicit policy for market-data outages while holding paper positions."""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MarketOutageDecision:
    new_entries_allowed: bool = False
    existing_positions_action: str = "RETAIN_OPEN"
    mark_dependent_protection: str = "PAUSE_UNTIL_FRESH_VALID_MARK"
    position_state: str = "MARK_STALE_UNVERIFIED"
    orders_authorized: bool = False


def market_outage_decision(*, feed_available: bool, mark_fresh: bool) -> MarketOutageDecision:
    """Never invent a close price or close on a stale mark.

    During an outage, retain durable positions, block new entries, mark the
    position state stale/unverified, and do not trigger price-based simulated
    stop/take-profit exits until a fresh valid mark returns. This is a paper
    policy; it is not an exchange-side protective order and offers no live safety.
    """
    if feed_available and mark_fresh:
        return MarketOutageDecision(
            new_entries_allowed=True,
            existing_positions_action="CONTINUE_WITH_FRESH_MARKS",
            mark_dependent_protection="EVALUATE_ON_VALID_MARK",
            position_state="MARK_FRESH",
            orders_authorized=False,
        )
    return MarketOutageDecision()
