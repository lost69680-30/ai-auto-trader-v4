from dataclasses import replace

from app.core.models import MarketSnapshot, RiskLimits, TradeProposal
from app.engines.orchestrator import (
    InMemoryPaperLedger,
    OrchestrationRequest,
    PaperOrderIntent,
    TradingOrchestrator,
)
from app.engines.paper_ledger import SQLitePaperLedger
from backend.signal_validation import SignalValidationResult
from backend.signal_watcher import WatchResult


def make_request(*, validation_status="VALID", watcher_status="TRIGGERED"):
    proposal = TradeProposal(
        proposal_id="proposal-1",
        symbol="BTCUSDT",
        side="LONG",
        quantity=0.01,
        entry_price=50000,
        stop_loss=49000,
        take_profit=52000,
        estimated_risk=10,
        signal_timestamp=1000,
    )
    market = MarketSnapshot("BTCUSDT", 50000, 1000, True)
    validation = SignalValidationResult(
        signal_id="signal-1",
        symbol="BTCUSDT",
        direction="LONG",
        status=validation_status,
        reasons=(),
        agreeing_strategies=("trend", "momentum"),
        opposing_strategies=(),
        gross_reward_risk=2,
        net_reward_risk=1.8,
    )
    return OrchestrationRequest(
        proposal=proposal,
        market=market,
        validation=validation,
        watch_result=WatchResult(
            watch_id="watch-1",
            signal_id="signal-1",
            symbol="BTCUSDT",
            status=watcher_status,
            reason="test watcher result",
            notify=watcher_status == "TRIGGERED",
            observed_price=50000,
        ),
        limits=RiskLimits(1000, 100, 3, 20),
        daily_pnl=0,
        open_positions=0,
        now=1000,
        initial_equity=10_000,
    )


def test_valid_triggered_signal_passes_risk_and_records_paper_order():
    ledger = InMemoryPaperLedger()
    result = TradingOrchestrator(ledger).process(make_request())

    assert result.status == "PAPER_ORDER_RECORDED"
    assert result.risk_decision.approved is True
    assert len(ledger.orders) == 1
    assert ledger.orders[0].proposal_id == "proposal-1"
    assert result.orders_authorized is False


def test_invalid_signal_fails_closed_before_risk_or_ledger():
    ledger = InMemoryPaperLedger()
    result = TradingOrchestrator(ledger).process(make_request(validation_status="REJECT"))

    assert result.status == "SIGNAL_REJECTED"
    assert result.risk_decision is None
    assert ledger.orders == ()


def test_signal_waits_until_watcher_triggered():
    ledger = InMemoryPaperLedger()
    result = TradingOrchestrator(ledger).process(make_request(watcher_status="WAITING"))

    assert result.status == "WAITING_FOR_TRIGGER"
    assert ledger.orders == ()


def test_risk_veto_never_records_paper_order():
    ledger = InMemoryPaperLedger()
    request = make_request()
    request = replace(request, daily_pnl=-100)

    result = TradingOrchestrator(ledger).process(request)

    assert result.status == "RISK_VETOED"
    assert result.risk_decision.code == "DAILY_LOSS_LIMIT"
    assert ledger.orders == ()


def test_repeated_proposal_is_idempotent():
    ledger = InMemoryPaperLedger()
    orchestrator = TradingOrchestrator(ledger)
    first = orchestrator.process(make_request())
    second_request = make_request()
    second = orchestrator.process(replace(second_request, now=1001))

    assert first.status == "PAPER_ORDER_RECORDED"
    assert second.status == "DUPLICATE_IGNORED"
    assert len(ledger.orders) == 1


def test_watcher_for_different_signal_fails_closed():
    ledger = InMemoryPaperLedger()
    request = make_request()
    mismatched_watch = WatchResult(
        watch_id="watch-2",
        signal_id="another-signal",
        symbol="BTCUSDT",
        status="TRIGGERED",
        reason="triggered",
        notify=True,
        observed_price=50000,
    )
    result = TradingOrchestrator(ledger).process(replace(request, watch_result=mismatched_watch))

    assert result.status == "SIGNAL_REJECTED"
    assert ledger.orders == ()


def test_conflicting_reuse_of_proposal_id_fails_closed():
    ledger = InMemoryPaperLedger()
    orchestrator = TradingOrchestrator(ledger)
    first = orchestrator.process(make_request())
    changed = make_request()
    changed_proposal = replace(changed.proposal, quantity=0.005)
    second = orchestrator.process(replace(changed, proposal=changed_proposal))

    assert first.status == "PAPER_ORDER_RECORDED"
    assert second.status == "IDEMPOTENCY_CONFLICT"
    assert len(ledger.orders) == 1


def test_watcher_cannot_authorize_orders():
    ledger = InMemoryPaperLedger()
    request = make_request()
    unsafe_watch = replace(request.watch_result, orders_authorized=True)
    result = TradingOrchestrator(ledger).process(replace(request, watch_result=unsafe_watch))

    assert result.status == "SIGNAL_REJECTED"
    assert ledger.orders == ()


def test_durable_emergency_stop_blocks_orchestrator_intent(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    ledger.activate_emergency_stop(occurred_at=999, reason="test stop")

    result = TradingOrchestrator(ledger).process(make_request())

    assert result.status == "EMERGENCY_STOP_ACTIVE"
    assert result.paper_intent is None
    assert ledger.orders == ()
    assert ledger.orders_authorized is False
    ledger.close()


def test_end_to_end_validated_signal_and_confirmed_trigger_reach_paper_intent_only():
    from datetime import datetime, timedelta, timezone

    from app.engines.research_pipeline import ResearchCandidate
    from app.engines.signal_handoff import SignalPricePlan, prepare_signal
    from backend.strategy_research import ResearchResult, StrategyAssessment
    from backend.signal_watcher import MarketTick, SignalWatcher

    now_dt = datetime(2026, 10, 10, 7, 0, tzinfo=timezone.utc)
    now_ts = now_dt.timestamp()
    research = ResearchResult(
        symbol="BTCUSDT", decision="REVIEW_LONG",
        assessments=(
            StrategyAssessment("trend_following", "LONG", 80, ("aligned",)),
            StrategyAssessment("momentum", "LONG", 75, ("aligned",)),
            StrategyAssessment("breakout", "LONG", 70, ("aligned",)),
            StrategyAssessment("mean_reversion", "NEUTRAL", 0, ("neutral",)),
        ),
        reasons=("research agreement",),
    )
    candidate = ResearchCandidate(
        symbol="BTCUSDT", scanner_score=90, status="REVIEW", direction="LONG",
        reasons=("engines aligned",), opportunity=None,
        strategy_research=research, liquidity=None,
    )
    plan = SignalPricePlan(
        signal_id="e2e-signal", direction="LONG", created_at=now_dt,
        expires_at=now_dt + timedelta(minutes=2), market_data_at=now_dt,
        entry_price=100, stop_price=98, target_price=105,
        estimated_round_trip_cost_pct=0.1, trigger_price=100,
        trigger_rule="AT_OR_ABOVE", invalidation_price=97,
    )
    handoff = prepare_signal(candidate, plan, now=now_dt)
    assert handoff.status == "VALID"
    assert handoff.watch_request is not None

    watcher = SignalWatcher()
    watcher.add(handoff.watch_request)
    watch_result = watcher.evaluate(
        MarketTick("BTCUSDT", 100, now_dt), now=now_dt
    )
    assert watch_result.status == "TRIGGERED"

    proposal = TradeProposal(
        proposal_id="e2e-proposal", symbol="BTCUSDT", side="LONG", quantity=0.01,
        entry_price=100, stop_loss=98, take_profit=105, estimated_risk=0.02,
        signal_timestamp=now_ts,
    )
    request = OrchestrationRequest(
        proposal=proposal,
        market=MarketSnapshot("BTCUSDT", 100, now_ts, True),
        validation=handoff.validation,
        watch_result=watch_result,
        limits=RiskLimits(1000, 100, 3, 20),
        daily_pnl=0, open_positions=0, now=now_ts, initial_equity=1_000,
    )
    ledger = InMemoryPaperLedger()
    result = TradingOrchestrator(ledger).process(request)

    assert result.status == "PAPER_ORDER_RECORDED"
    assert len(ledger.orders) == 1
    assert result.orders_authorized is False


def test_market_gate_connects_healthy_worker_frame_to_real_paper_orchestrator():
    from datetime import datetime, timezone
    from app.engines.market_paper_gate import MarketPaperGate
    from backend.market_worker import FeedStatus, MarketFrame

    at = datetime.fromtimestamp(1000, tz=timezone.utc)
    frame = MarketFrame(
        symbol="BTCUSDT", status=FeedStatus.HEALTHY, checked_at=at,
        mark_price=50000, mark_observed_at=at, closed_candles=(object(),),
        reasons=(), retry_count=0,
    )
    ledger = InMemoryPaperLedger()
    orchestrator = TradingOrchestrator(ledger)
    gate = MarketPaperGate(orchestrator.process, max_age_seconds=90)

    result = gate.process(frame, make_request(), now=1000)

    assert result.status == "PAPER_ORDER_RECORDED"
    assert result.result is not None and result.result.risk_decision.approved
    assert len(ledger.orders) == 1
    assert result.orders_authorized is False


def test_market_gate_blocks_real_orchestrator_when_worker_frame_is_unhealthy():
    from datetime import datetime, timezone
    from app.engines.market_paper_gate import MarketPaperGate
    from backend.market_worker import FeedStatus, MarketFrame

    at = datetime.fromtimestamp(1000, tz=timezone.utc)
    frame = MarketFrame(
        symbol="BTCUSDT", status=FeedStatus.DISCONNECTED, checked_at=at,
        mark_price=None, mark_observed_at=None, closed_candles=(),
        reasons=("network unavailable",), retry_count=1,
    )
    ledger = InMemoryPaperLedger()
    gate = MarketPaperGate(TradingOrchestrator(ledger).process)

    result = gate.process(frame, make_request(), now=1000)

    assert result.status == "MARKET_DATA_BLOCKED"
    assert ledger.orders == ()
    assert result.orders_authorized is False


def test_durable_ledger_daily_loss_overrides_caller_supplied_pnl(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "ledger-risk-gate.db")
    losing = PaperOrderIntent(
        proposal_id="prior-loss", signal_id="prior-signal", symbol="BTCUSDT", side="LONG",
        quantity=2.0, entry_price=100.0, stop_loss=90.0, take_profit=120.0, recorded_at=500,
    )
    assert ledger.record_intent(losing) == "RECORDED"
    assert ledger.simulate_fill("prior-loss", market_price=100.0, filled_at=501,
                                fee_rate=0, slippage_bps=0).status == "FILLED"
    assert ledger.close_position("prior-loss", market_price=50.0, closed_at=700,
                                 fee_rate=0, slippage_bps=0).status == "CLOSED"

    request = make_request()
    request = replace(request, daily_pnl=0.0, initial_equity=10_000,
                      limits=RiskLimits(max_position_notional=100_000, max_daily_loss=100.0, max_open_positions=5, max_risk_per_trade=1_000.0, max_signal_age_seconds=20.0))
    result = TradingOrchestrator(ledger).process(request)
    assert result.status == "RISK_VETOED"
    assert result.risk_decision is not None
    assert result.risk_decision.code == "DAILY_LOSS_LIMIT"
    assert ledger.orders_authorized is False
    assert all(order.proposal_id != request.proposal.proposal_id for order in ledger.orders)
    ledger.close()


def test_durable_ledger_fails_closed_without_equity_baseline(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "missing-baseline.db")
    result = TradingOrchestrator(ledger).process(replace(make_request(), initial_equity=None))
    assert result.status == "RISK_VETOED"
    assert "initial-equity baseline" in result.reason
    assert ledger.orders == ()
    ledger.close()


def test_durable_ledger_fails_closed_when_open_position_marks_are_stale(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "stale-open-mark.db")
    intent = PaperOrderIntent(
        proposal_id="existing-stale", signal_id="existing-signal", symbol="BTCUSDT", side="LONG",
        quantity=0.1, entry_price=50_000, stop_loss=49_000, take_profit=52_000, recorded_at=900,
    )
    assert ledger.record_intent(intent) == "RECORDED"
    assert ledger.simulate_fill(
        "existing-stale", market_price=50_000, filled_at=901, fee_rate=0, slippage_bps=0,
    ).status == "FILLED"

    request = replace(
        make_request(),
        limits=RiskLimits(100_000, 1_000, 5, 1_000, max_mark_age_seconds=30),
        initial_equity=10_000,
        now=1_000,
    )
    result = TradingOrchestrator(ledger).process(request)

    assert result.status == "RISK_VETOED"
    assert "mark data is stale" in result.reason
    assert all(order.proposal_id != request.proposal.proposal_id for order in ledger.orders)
    assert ledger.orders_authorized is False
    ledger.close()


def test_durable_ledger_blocks_new_risk_at_mark_to_market_drawdown_limit(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "drawdown-veto.db")
    intent = PaperOrderIntent(
        proposal_id="existing-loss", signal_id="existing-signal", symbol="BTCUSDT", side="LONG",
        quantity=0.1, entry_price=50_000, stop_loss=49_000, take_profit=52_000, recorded_at=900,
    )
    assert ledger.record_intent(intent) == "RECORDED"
    assert ledger.simulate_fill(
        "existing-loss", market_price=50_000, filled_at=901, fee_rate=0, slippage_bps=0,
    ).status == "FILLED"
    ledger.mark_positions({"BTCUSDT": 25_000}, observed_at=995)

    request = replace(
        make_request(),
        limits=RiskLimits(100_000, 1_000, 5, 1_000, max_drawdown_pct=20, max_mark_age_seconds=30),
        initial_equity=10_000,
        now=1_000,
    )
    result = TradingOrchestrator(ledger).process(request)

    assert result.status == "RISK_VETOED"
    assert "drawdown limit reached" in result.reason
    assert all(order.proposal_id != request.proposal.proposal_id for order in ledger.orders)
    assert ledger.orders_authorized is False
    ledger.close()
