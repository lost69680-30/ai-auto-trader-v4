from datetime import datetime, timezone
from types import SimpleNamespace

from app.core.models import MarketSnapshot
from app.engines.market_paper_gate import MarketPaperGate
from app.engines.paper_trading_service import PaperTradingService
from backend.market_worker import FeedStatus, MarketFrame, MarketPollResult

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def market_frame(symbol="BTCUSDT", status=FeedStatus.HEALTHY, price=100.0):
    return MarketFrame(
        symbol=symbol, status=status, checked_at=NOW, mark_price=price,
        mark_observed_at=NOW, closed_candles=(object(),), reasons=(), retry_count=0,
    )


def test_cycle_routes_healthy_frames_to_gate_and_reports_blocked_frames():
    processed = []

    def make_request(frame):
        return SimpleNamespace(market=MarketSnapshot(frame.symbol, frame.mark_price, NOW.timestamp(), True))

    service = PaperTradingService(
        gate=MarketPaperGate(lambda request: processed.append(request) or SimpleNamespace(status="PAPER_ORDER_RECORDED")),
        request_factory=make_request,
    )
    poll = MarketPollResult(NOW, (
        market_frame(),
        market_frame("ETHUSDT", FeedStatus.DISCONNECTED, 20.0),
    ))

    report = service.process_poll(poll, now=NOW.timestamp())

    assert [item.symbol for item in report.items] == ["BTCUSDT", "ETHUSDT"]
    assert [item.status for item in report.items] == ["PAPER_ORDER_RECORDED", "MARKET_DATA_BLOCKED"]
    assert len(processed) == 1
    assert report.orders_authorized is False


def test_request_factory_failure_is_contained_and_fails_closed():
    service = PaperTradingService(
        gate=MarketPaperGate(lambda request: (_ for _ in ()).throw(AssertionError("must not process"))),
        request_factory=lambda frame: 1 / 0,
    )
    report = service.process_poll(MarketPollResult(NOW, (market_frame(),)), now=NOW.timestamp())

    assert report.items[0].status == "REQUEST_BUILD_FAILED"
    assert report.items[0].orders_authorized is False


def test_duplicate_symbol_in_poll_is_rejected_without_second_processing():
    calls = []
    service = PaperTradingService(
        gate=MarketPaperGate(lambda request: calls.append(request) or SimpleNamespace(status="PAPER_ORDER_RECORDED")),
        request_factory=lambda frame: SimpleNamespace(market=MarketSnapshot(frame.symbol, frame.mark_price, NOW.timestamp(), True)),
    )
    poll = MarketPollResult(NOW, (market_frame(), market_frame()))

    report = service.process_poll(poll, now=NOW.timestamp())

    assert report.items[0].status == "PAPER_ORDER_RECORDED"
    assert report.items[1].status == "DUPLICATE_SYMBOL_REJECTED"
    assert len(calls) == 1


def test_service_integrates_real_orchestrator_and_paper_ledger():
    from dataclasses import replace

    from app.core.models import MarketSnapshot
    from app.engines.orchestrator import InMemoryPaperLedger, TradingOrchestrator
    from tests.test_orchestrator import make_request

    ledger = InMemoryPaperLedger()
    orchestrator = TradingOrchestrator(ledger)
    request = make_request()
    request = replace(
        request,
        market=MarketSnapshot("BTCUSDT", 50000, NOW.timestamp(), True),
        proposal=replace(request.proposal, signal_timestamp=NOW.timestamp()),
        now=NOW.timestamp(),
    )
    frame = market_frame(price=50000)
    service = PaperTradingService(
        gate=MarketPaperGate(orchestrator.process),
        request_factory=lambda _: request,
    )

    report = service.process_poll(MarketPollResult(NOW, (frame,)), now=NOW.timestamp())

    assert report.items[0].status == "PAPER_ORDER_RECORDED"
    assert len(ledger.orders) == 1
    assert ledger.orders[0].proposal_id == "proposal-1"
    assert report.orders_authorized is False


def test_unexpected_poll_authorization_flag_blocks_every_frame():
    calls = []
    service = PaperTradingService(
        gate=MarketPaperGate(lambda request: calls.append(request) or "unsafe"),
        request_factory=lambda frame: SimpleNamespace(market=MarketSnapshot(frame.symbol, frame.mark_price, NOW.timestamp(), True)),
    )
    poll = MarketPollResult(NOW, (market_frame(),), orders_authorized=True)

    report = service.process_poll(poll, now=NOW.timestamp())

    assert report.items[0].status == "AUTHORIZATION_FLAG_REJECTED"
    assert calls == []
    assert report.orders_authorized is False
