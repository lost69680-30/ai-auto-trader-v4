from dataclasses import replace
from datetime import datetime, timezone

from app.core.models import MarketSnapshot
from app.engines.orchestrator import InMemoryPaperLedger, TradingOrchestrator
from app.engines.paper_signal_cycle import PaperSignalCycle
from tests.test_orchestrator import make_request
from tests.test_signal_handoff import candidate, plan
from app.engines.signal_handoff import prepare_signal
from backend.signal_watcher import MarketTick, SignalWatcher

NOW = datetime(2026, 10, 10, 7, 0, tzinfo=timezone.utc)


def setup_cycle():
    handoff = prepare_signal(candidate(), plan(), now=NOW)
    ledger = InMemoryPaperLedger()
    cycle = PaperSignalCycle(TradingOrchestrator(ledger), SignalWatcher())
    request = make_request()
    request = replace(
        request,
        market=MarketSnapshot("BTCUSDT", 100, NOW.timestamp(), True),
        proposal=replace(request.proposal, entry_price=100, stop_loss=98, take_profit=105,
                         signal_timestamp=NOW.timestamp()),
        now=NOW.timestamp(),
    )
    tick = MarketTick("BTCUSDT", 99, NOW)
    return cycle, handoff, request, tick, ledger


def test_validated_signal_waits_then_trigger_records_one_paper_intent():
    cycle, handoff, request, tick, ledger = setup_cycle()
    first = cycle.process(handoff, request, tick, now=NOW)
    assert first.status == "WAITING_FOR_TRIGGER"
    assert ledger.orders == ()

    triggered = MarketTick("BTCUSDT", 101, NOW)
    second = cycle.process(handoff, request, triggered, now=NOW)
    assert second.status == "PAPER_ORDER_RECORDED"
    assert len(ledger.orders) == 1
    assert second.orders_authorized is False


def test_mismatched_proposal_prices_are_rejected_before_watcher_or_ledger():
    cycle, handoff, request, tick, ledger = setup_cycle()
    bad = replace(request, proposal=replace(request.proposal, entry_price=101))
    result = cycle.process(handoff, bad, tick, now=NOW)
    assert result.status == "PAPER_CYCLE_REJECTED"
    assert ledger.orders == ()


def test_invalid_handoff_cannot_register_or_record_intent():
    cycle, handoff, request, tick, ledger = setup_cycle()
    bad_handoff = replace(handoff, status="REJECT", validation=replace(handoff.validation, status="REJECT"))
    result = cycle.process(bad_handoff, request, tick, now=NOW)
    assert result.status == "PAPER_CYCLE_REJECTED"
    assert ledger.orders == ()


def test_future_tick_is_rejected_without_ledger_mutation():
    cycle, handoff, request, _, ledger = setup_cycle()
    future_tick = MarketTick("BTCUSDT", 101, NOW.replace(year=2027))
    result = cycle.process(handoff, request, future_tick, now=NOW)
    assert result.status == "PAPER_CYCLE_REJECTED"
    assert ledger.orders == ()


def test_daily_loss_veto_prevents_paper_intent_after_trigger():
    cycle, handoff, request, _, ledger = setup_cycle()
    request = replace(request, daily_pnl=-100)
    result = cycle.process(
        handoff, request, MarketTick("BTCUSDT", 101, NOW), now=NOW
    )
    assert result.status == "RISK_VETOED"
    assert ledger.orders == ()
