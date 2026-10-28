from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from backend.market_worker import FeedStatus, MarketFrame
from app.engines.market_paper_gate import MarketPaperGate
from app.core.models import MarketSnapshot

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def frame(status=FeedStatus.HEALTHY, *, mark=101.0, candles=True, checked_at=NOW, observed_at=NOW):
    return MarketFrame(
        symbol="BTCUSDT", status=status, checked_at=checked_at,
        mark_price=mark, mark_observed_at=observed_at,
        closed_candles=(object(),) if candles else (), reasons=(), retry_count=0,
    )


def request(mark=101.0, observed_at=NOW.timestamp()):
    return SimpleNamespace(market=MarketSnapshot("BTCUSDT", mark, observed_at, True))


def test_healthy_fresh_matching_market_frame_reaches_paper_processor():
    calls = []
    gate = MarketPaperGate(lambda req: calls.append(req) or "PAPER_ONLY")

    result = gate.process(frame(), request(), now=NOW.timestamp())

    assert result.status == "PAPER_ONLY"
    assert len(calls) == 1
    assert result.orders_authorized is False


def test_unhealthy_frame_never_reaches_processor():
    calls = []
    gate = MarketPaperGate(lambda req: calls.append(req) or "PAPER_ONLY")

    result = gate.process(frame(FeedStatus.DISCONNECTED), request(), now=NOW.timestamp())

    assert result.status == "MARKET_DATA_BLOCKED"
    assert calls == []
    assert result.orders_authorized is False


def test_mark_price_mismatch_fails_closed():
    calls = []
    gate = MarketPaperGate(lambda req: calls.append(req) or "PAPER_ONLY")

    result = gate.process(frame(mark=101), request(mark=102), now=NOW.timestamp())

    assert result.status == "MARKET_DATA_BLOCKED"
    assert calls == []


def test_stale_or_candleless_frame_fails_closed():
    gate = MarketPaperGate(lambda req: "PAPER_ONLY", max_age_seconds=30)
    stale = frame(checked_at=NOW - timedelta(seconds=31), observed_at=NOW - timedelta(seconds=31))
    no_candles = frame(candles=False)

    assert gate.process(stale, request(observed_at=(NOW - timedelta(seconds=31)).timestamp()), now=NOW.timestamp()).status == "MARKET_DATA_BLOCKED"
    assert gate.process(no_candles, request(), now=NOW.timestamp()).status == "MARKET_DATA_BLOCKED"
