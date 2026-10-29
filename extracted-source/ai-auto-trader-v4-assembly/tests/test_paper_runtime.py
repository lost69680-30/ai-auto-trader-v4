from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.engines.paper_ledger import SQLitePaperLedger
from app.engines.protection import PaperProtectionEngine
from app.engines.paper_runtime import PaperRuntime
from backend.market_data_adapter import MarketCandle
from backend.market_worker import FeedStatus, MarketFrame, MarketPollResult

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def frame(symbol="BTCUSDT", status=FeedStatus.HEALTHY, price=100.0, observed_at=None, candle_close=None):
    observed_at = observed_at or NOW
    candle_close = candle_close or (observed_at - timedelta(seconds=1))
    candles = (MarketCandle(
        symbol, candle_close - timedelta(seconds=60), candle_close,
        99.0, 101.0, 98.0, 100.0, 10.0, 1000.0, 2, True,
    ),) if status is FeedStatus.HEALTHY else ()
    return MarketFrame(symbol, status, NOW, price if status is FeedStatus.HEALTHY else None,
                       observed_at if status is FeedStatus.HEALTHY else None, candles, (), 0)


def test_runtime_runs_analysis_only_on_healthy_frames_and_keeps_paper_only(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "runtime.db")
    seen = []
    runtime = PaperRuntime(
        ledger=ledger,
        protection=PaperProtectionEngine(ledger, fee_rate=0, slippage_bps=0),
        analysis_handler=lambda poll: seen.append(tuple(f.symbol for f in poll.frames)) or "ANALYZED",
        fee_rate=0,
        slippage_bps=0,
    )

    report = runtime.process_poll(MarketPollResult(NOW, (frame(), frame("ETHUSDT", FeedStatus.DISCONNECTED))), now=NOW.timestamp())

    assert seen == [("BTCUSDT",)]
    assert report.analysis_status == "ANALYZED"
    assert report.healthy_symbols == ("BTCUSDT",)
    assert report.unhealthy_symbols == ("ETHUSDT",)
    assert report.orders_authorized is False
    ledger.close()


def test_runtime_does_not_call_analysis_when_every_feed_is_unhealthy(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "runtime.db")
    seen = []
    runtime = PaperRuntime(ledger=ledger, protection=PaperProtectionEngine(ledger), analysis_handler=lambda poll: seen.append(poll))

    report = runtime.process_poll(MarketPollResult(NOW, (frame(status=FeedStatus.DISCONNECTED),)), now=NOW.timestamp())

    assert seen == []
    assert report.analysis_status == "SKIPPED_UNHEALTHY_MARKET_DATA"
    assert report.orders_authorized is False
    ledger.close()


def test_runtime_fills_only_preexisting_pending_paper_intents_and_updates_protection(tmp_path):
    from app.engines.orchestrator import PaperOrderIntent

    ledger = SQLitePaperLedger(tmp_path / "runtime.db")
    intent = PaperOrderIntent(
        proposal_id="p1", signal_id="s1", symbol="BTCUSDT", side="LONG", quantity=1,
        entry_price=100, stop_loss=95, take_profit=110, recorded_at=NOW.timestamp() - 10,
    )
    assert ledger.record_intent(intent) == "RECORDED"
    runtime = PaperRuntime(
        ledger=ledger, protection=PaperProtectionEngine(ledger, fee_rate=0, slippage_bps=0),
        analysis_handler=lambda poll: "ANALYZED", fee_rate=0, slippage_bps=0,
    )

    report = runtime.process_poll(MarketPollResult(NOW, (frame(price=100),)), now=NOW.timestamp())

    assert report.fill_statuses == (("p1", "FILLED"),)
    assert len(ledger.fills) == 1
    assert len(ledger.open_positions) == 1
    assert report.protection_status == "MARKET_UPDATED"
    assert report.orders_authorized is False
    ledger.close()


def test_runtime_does_not_fill_intent_created_by_current_analysis_poll(tmp_path):
    from app.engines.orchestrator import PaperOrderIntent

    ledger = SQLitePaperLedger(tmp_path / "runtime.db")
    runtime = PaperRuntime(
        ledger=ledger, protection=PaperProtectionEngine(ledger),
        analysis_handler=lambda poll: ledger.record_intent(PaperOrderIntent(
            proposal_id="new", signal_id="s-new", symbol="BTCUSDT", side="LONG", quantity=1,
            entry_price=100, stop_loss=95, take_profit=110, recorded_at=NOW.timestamp(),
        )),
    )
    report = runtime.process_poll(MarketPollResult(NOW, (frame(),)), now=NOW.timestamp())
    assert report.analysis_result == "RECORDED"
    assert report.fill_statuses == ()
    assert ledger.fills == ()
    ledger.close()


def test_runtime_rejects_duplicate_symbol_frames_before_analysis_or_marking(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "duplicates.db")
    seen = []
    runtime = PaperRuntime(
        ledger=ledger,
        protection=PaperProtectionEngine(ledger),
        analysis_handler=lambda poll: seen.append(poll) or "ANALYZED",
    )
    poll = MarketPollResult(NOW, (frame(), frame(price=101.0)), orders_authorized=False)

    report = runtime.process_poll(poll, now=NOW.timestamp())

    assert seen == []
    assert report.healthy_symbols == ()
    assert report.unhealthy_symbols == ("BTCUSDT", "BTCUSDT")
    assert report.analysis_status == "SKIPPED_UNHEALTHY_MARKET_DATA"
    assert report.protection_status == "SKIPPED_UNHEALTHY_MARKET_DATA"
    ledger.close()


def test_runtime_rejects_stale_poll_even_when_frame_claims_fresh_mark(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "stale-poll.db")
    seen = []
    runtime = PaperRuntime(
        ledger=ledger,
        protection=PaperProtectionEngine(ledger),
        analysis_handler=lambda poll: seen.append(poll) or "ANALYZED",
    )
    stale_poll = MarketPollResult(NOW - timedelta(minutes=2), (frame(),), orders_authorized=False)

    report = runtime.process_poll(stale_poll, now=NOW.timestamp())

    assert seen == []
    assert report.healthy_symbols == ()
    assert report.unhealthy_symbols == ("BTCUSDT",)
    assert report.analysis_status == "SKIPPED_UNHEALTHY_MARKET_DATA"
    ledger.close()


def test_runtime_rejects_frame_without_real_closed_candles(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "bad-candles.db")
    seen = []
    runtime = PaperRuntime(
        ledger=ledger,
        protection=PaperProtectionEngine(ledger),
        analysis_handler=lambda poll: seen.append(poll) or "ANALYZED",
    )
    invalid = MarketFrame(
        "BTCUSDT", FeedStatus.HEALTHY, NOW, 100.0, NOW, (object(),), (), 0,
    )

    report = runtime.process_poll(MarketPollResult(NOW, (invalid,)), now=NOW.timestamp())

    assert seen == []
    assert report.healthy_symbols == ()
    assert report.unhealthy_symbols == ("BTCUSDT",)
    ledger.close()


def test_runtime_rejects_non_finite_candle_values(tmp_path):
    import math

    ledger = SQLitePaperLedger(tmp_path / "nan-candle.db")
    seen = []
    runtime = PaperRuntime(
        ledger=ledger,
        protection=PaperProtectionEngine(ledger),
        analysis_handler=lambda poll: seen.append(poll) or "ANALYZED",
    )
    bad_candle = MarketCandle(
        "BTCUSDT", NOW - timedelta(seconds=61), NOW - timedelta(seconds=1),
        99.0, math.nan, 98.0, 100.0, 10.0, 1000.0, 2, True,
    )
    bad_frame = MarketFrame("BTCUSDT", FeedStatus.HEALTHY, NOW, 100.0, NOW, (bad_candle,), (), 0)

    report = runtime.process_poll(MarketPollResult(NOW, (bad_frame,)), now=NOW.timestamp())

    assert seen == []
    assert report.healthy_symbols == ()
    assert report.unhealthy_symbols == ("BTCUSDT",)
    ledger.close()


def test_runtime_preserves_market_observation_time_for_protection(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "market-time.db")
    protection = PaperProtectionEngine(ledger)
    seen_times = []
    original_update = protection.process_market_update

    def capture_update(prices, *, observed_at):
        seen_times.append(observed_at)
        return original_update(prices, observed_at=observed_at)

    protection.process_market_update = capture_update
    runtime = PaperRuntime(
        ledger=ledger, protection=protection, analysis_handler=lambda poll: "ANALYZED",
    )
    quote_time = NOW - timedelta(seconds=5)
    quote_frame = frame(observed_at=quote_time, candle_close=quote_time - timedelta(seconds=1))

    runtime.process_poll(MarketPollResult(NOW, (quote_frame,)), now=NOW.timestamp())

    assert seen_times == [quote_time.timestamp()]
    assert seen_times[0] < NOW.timestamp()
    ledger.close()


def test_runtime_does_not_fill_pending_intent_from_quote_observed_before_intent(tmp_path):
    from app.engines.orchestrator import PaperOrderIntent

    ledger = SQLitePaperLedger(tmp_path / "pre-intent-quote.db")
    intent = PaperOrderIntent(
        proposal_id="p-old-quote", signal_id="s-old-quote", symbol="BTCUSDT", side="LONG",
        quantity=1, entry_price=100, stop_loss=95, take_profit=110,
        recorded_at=NOW.timestamp() - 2,
    )
    assert ledger.record_intent(intent) == "RECORDED"
    runtime = PaperRuntime(
        ledger=ledger, protection=PaperProtectionEngine(ledger),
        analysis_handler=lambda poll: "ANALYZED", fee_rate=0, slippage_bps=0,
    )
    quote_time = NOW - timedelta(seconds=5)
    quote_frame = frame(observed_at=quote_time, candle_close=quote_time - timedelta(seconds=1))

    report = runtime.process_poll(MarketPollResult(NOW, (quote_frame,)), now=NOW.timestamp())

    assert report.fill_statuses == ()
    assert ledger.fills == ()
    assert ledger.open_positions == ()
    ledger.close()


def test_runtime_rejects_candle_that_closes_after_its_mark_observation(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "candle-after-mark.db")
    seen = []
    runtime = PaperRuntime(
        ledger=ledger, protection=PaperProtectionEngine(ledger),
        analysis_handler=lambda poll: seen.append(poll) or "ANALYZED",
    )
    quote_time = NOW - timedelta(seconds=5)
    inconsistent = frame(observed_at=quote_time, candle_close=NOW - timedelta(seconds=1))

    report = runtime.process_poll(MarketPollResult(NOW, (inconsistent,)), now=NOW.timestamp())

    assert seen == []
    assert report.healthy_symbols == ()
    assert report.unhealthy_symbols == ("BTCUSDT",)
    ledger.close()


def test_runtime_blocks_pending_fill_when_analysis_handler_reports_provider_failure(tmp_path):
    from types import SimpleNamespace
    from app.engines.orchestrator import PaperOrderIntent

    ledger = SQLitePaperLedger(tmp_path / "analysis-failure.db")
    intent = PaperOrderIntent(
        proposal_id="blocked-on-provider-error", signal_id="provider-error-signal",
        symbol="BTCUSDT", side="LONG", quantity=0.1, entry_price=100,
        stop_loss=95, take_profit=110, recorded_at=NOW.timestamp() - 10,
    )
    assert ledger.record_intent(intent) == "RECORDED"
    runtime = PaperRuntime(
        ledger=ledger, protection=PaperProtectionEngine(ledger),
        analysis_handler=lambda poll: SimpleNamespace(
            status="MARKET_UNIVERSE_UNAVAILABLE", reason="public market universe unavailable",
        ),
    )

    report = runtime.process_poll(MarketPollResult(NOW, (frame(),), orders_authorized=False), now=NOW.timestamp())

    assert report.analysis_status == "MARKET_UNIVERSE_UNAVAILABLE"
    assert report.fill_statuses == ()
    assert ledger.fills == ()
    assert ledger.orders[0].status == "PENDING_PAPER_SIMULATION"
    assert report.orders_authorized is False
    ledger.close()


def test_runtime_only_fills_symbols_eligible_after_analysis_exclusions(tmp_path):
    from types import SimpleNamespace
    from app.engines.orchestrator import PaperOrderIntent

    ledger = SQLitePaperLedger(tmp_path / "analysis-exclusions.db")
    for symbol in ("BTCUSDT", "ETHUSDT"):
        intent = PaperOrderIntent(
            proposal_id=f"eligible-{symbol}", signal_id=f"eligible-signal-{symbol}",
            symbol=symbol, side="LONG", quantity=0.1, entry_price=100,
            stop_loss=95, take_profit=110, recorded_at=NOW.timestamp() - 10,
        )
        assert ledger.record_intent(intent) == "RECORDED"
    runtime = PaperRuntime(
        ledger=ledger, protection=PaperProtectionEngine(ledger),
        analysis_handler=lambda poll: SimpleNamespace(
            status="ANALYZED_WITH_EXCLUSIONS", eligible_symbols=("ETHUSDT",),
        ),
    )

    report = runtime.process_poll(
        MarketPollResult(NOW, (frame("BTCUSDT"), frame("ETHUSDT")), orders_authorized=False),
        now=NOW.timestamp(),
    )

    assert report.analysis_status == "ANALYZED_WITH_EXCLUSIONS"
    assert report.fill_statuses == (("eligible-ETHUSDT", "FILLED"),)
    assert {position.symbol for position in ledger.open_positions} == {"ETHUSDT"}
    assert ledger.orders_authorized is False
    ledger.close()
