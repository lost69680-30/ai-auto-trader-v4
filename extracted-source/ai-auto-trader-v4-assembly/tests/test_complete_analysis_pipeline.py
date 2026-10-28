from datetime import datetime, timezone
from types import SimpleNamespace

from app.engines.complete_analysis_pipeline import CompleteAnalysisPipeline

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def test_incomplete_research_never_calls_price_planner_or_request_builder():
    calls = []
    pipeline = CompleteAnalysisPipeline(
        plan_request_factory=lambda candidate: calls.append(("plan", candidate)),
        orchestration_request_factory=lambda candidate, plan: calls.append(("request", candidate, plan)),
        signal_cycle=SimpleNamespace(process=lambda *args, **kwargs: calls.append(("cycle", args))),
    )
    research = SimpleNamespace(
        candidates=(SimpleNamespace(symbol="BTCUSDT", status="NO_TRADE", direction="NONE"),),
        rejections=(), orders_authorized=False,
    )
    report = pipeline.process_research(
        research, ticks={}, now=NOW,
    )
    assert report.items[0].status == "NO_TRADE"
    assert calls == []
    assert report.orders_authorized is False


def test_unexpected_research_authorization_flag_blocks_entire_pipeline():
    calls = []
    pipeline = CompleteAnalysisPipeline(
        plan_request_factory=lambda candidate: calls.append(candidate),
        orchestration_request_factory=lambda candidate, plan: None,
        signal_cycle=SimpleNamespace(process=lambda *args, **kwargs: None),
    )
    research = SimpleNamespace(candidates=(), rejections=(), orders_authorized=True)
    report = pipeline.process_research(research, ticks={}, now=NOW)
    assert report.status == "RESEARCH_AUTHORIZATION_FLAG_REJECTED"
    assert calls == []


def test_review_candidate_flows_through_real_atr_planner_and_signal_handoff():
    from types import SimpleNamespace
    from backend.signal_watcher import MarketTick
    from tests.test_signal_handoff import candidate
    from tests.test_price_planner import request as planner_request

    cycle_calls = []

    class Cycle:
        def process(self, handoff, request, tick, *, now):
            cycle_calls.append((handoff, request, tick, now))
            return SimpleNamespace(status="PAPER_ORDER_RECORDED", reason="test paper-only downstream")

    row = candidate()
    pipeline = CompleteAnalysisPipeline(
        plan_request_factory=lambda _: planner_request(),
        orchestration_request_factory=lambda c, handoff: SimpleNamespace(candidate=c, handoff=handoff),
        signal_cycle=Cycle(),
    )
    # The planner's real ATR output defines the trigger, so compute it once for
    # this test tick. This is test setup, not a production bypass.
    from app.engines.price_planner import plan_prices
    plan = plan_prices(planner_request()).plan
    assert plan is not None
    tick = MarketTick("BTCUSDT", plan.trigger_price, NOW, candle_close=plan.trigger_price)
    report = pipeline.process_research(
        SimpleNamespace(candidates=(row,), rejections=(), orders_authorized=False),
        ticks={"BTCUSDT": tick}, now=NOW,
    )
    assert report.items[0].status == "PAPER_ORDER_RECORDED"
    assert cycle_calls and cycle_calls[0][0].status == "VALID"
    assert report.orders_authorized is False
