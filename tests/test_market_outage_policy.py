from app.engines.market_outage_policy import market_outage_decision


def test_outage_retains_existing_positions_and_blocks_new_entries():
    decision = market_outage_decision(feed_available=False, mark_fresh=False)
    assert decision.new_entries_allowed is False
    assert decision.existing_positions_action == "RETAIN_OPEN"
    assert decision.mark_dependent_protection == "PAUSE_UNTIL_FRESH_VALID_MARK"
    assert decision.position_state == "MARK_STALE_UNVERIFIED"
    assert decision.orders_authorized is False


def test_feed_return_requires_fresh_valid_mark_before_protection_resumes():
    stale = market_outage_decision(feed_available=True, mark_fresh=False)
    assert stale.new_entries_allowed is False
    fresh = market_outage_decision(feed_available=True, mark_fresh=True)
    assert fresh.new_entries_allowed is True
    assert fresh.mark_dependent_protection == "EVALUATE_ON_VALID_MARK"
    assert fresh.orders_authorized is False
