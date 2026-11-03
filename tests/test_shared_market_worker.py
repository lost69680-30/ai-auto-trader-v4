import pytest

from backend import shared_market_worker


class FakeStore:
    released = False

    def __init__(self, *args, **kwargs):
        pass

    def acquire_lease(self):
        return True

    def release(self):
        type(self).released = True


def test_scheduler_startup_failure_releases_market_worker_lease(monkeypatch):
    FakeStore.released = False
    monkeypatch.setenv("PAPER_DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(shared_market_worker, "PostgresMarketFeedStore", FakeStore)

    def fail_scheduler(*args, **kwargs):
        raise RuntimeError("scheduler database unavailable")

    monkeypatch.setattr(shared_market_worker, "DurableJobScheduler", fail_scheduler)

    with pytest.raises(RuntimeError, match="scheduler database unavailable"):
        shared_market_worker.main()

    assert FakeStore.released is True

def test_scheduled_job_runner_executes_off_feed_loop_and_stops_cleanly():
    import threading

    class Scheduler:
        def __init__(self):
            self.called = threading.Event()

        def enqueue_due(self):
            self.called.set()

    class Dispatcher:
        def __init__(self):
            self.called = threading.Event()
            self.last_run_status = "SUCCEEDED"
            self.last_error = None

        def run_once(self):
            self.called.set()
            return True

    stop = threading.Event()
    scheduler = Scheduler()
    dispatcher = Dispatcher()
    health = {"status": "STARTING"}
    health_lock = threading.Lock()
    thread = threading.Thread(
        target=shared_market_worker.run_scheduled_jobs,
        args=(stop, scheduler, dispatcher),
        kwargs={"poll_interval": 0.01, "health": health, "health_lock": health_lock},
        daemon=True,
    )
    thread.start()
    assert scheduler.called.wait(1.0)
    assert dispatcher.called.wait(1.0)
    stop.set()
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert health["status"] == "RUNNING"
    assert health["last_dispatch_status"] == "SUCCEEDED"
    assert health["last_error"] is None
    assert health["last_poll_at"] is not None


def test_scheduled_job_runner_surfaces_database_failure_in_health():
    import threading

    stop = threading.Event()
    health = {"status": "STARTING"}
    health_lock = threading.Lock()

    class BrokenScheduler:
        def enqueue_due(self):
            stop.set()
            raise RuntimeError("database unavailable")

    class Dispatcher:
        last_run_status = "IDLE"
        last_error = None

        def run_once(self):
            return False

    shared_market_worker.run_scheduled_jobs(
        stop, BrokenScheduler(), Dispatcher(),
        poll_interval=0.01, health=health, health_lock=health_lock,
    )
    assert health["status"] == "DEGRADED"
    assert health["last_error"] == "RuntimeError"
    assert health["last_dispatch_status"] == "FAILED"


def test_opportunity_reassessment_handler_uses_venue_scoped_read_only_price_source():
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from backend.opportunity_coordinator import OpportunityCoordinator
    from backend.shared_market_worker import opportunity_reassessment_handler

    coordinator = object.__new__(OpportunityCoordinator)
    coordinator.market_type = "SPOT"
    coordinator.venue = "BINANCE_SPOT_TESTNET"
    seen = {}

    def reassess_due(*, now, market_update_provider, limit):
        seen["limit"] = limit
        seen["now"] = now
        tick = market_update_provider("BTCUSDT", "1h", now)
        seen["tick"] = tick
        return SimpleNamespace(evaluated_count=1, failed_count=0, orders_authorized=False)

    coordinator.reassess_due = reassess_due

    class SpotSource:
        def fetch_mark_price(self, symbol):
            return SimpleNamespace(
                symbol=symbol, mark_price=123.4,
                observed_at=datetime.now(timezone.utc),
                orders_authorized=False, market_type="SPOT",
            )

        def fetch_klines(self, symbol, *, interval, limit, now):
            seen["candle_request"] = (symbol, interval, limit)
            return (SimpleNamespace(
                symbol=symbol, close_time=now, close=123.1, is_closed=True,
            ),)

    handler = opportunity_reassessment_handler(coordinator, SpotSource())
    handler(
        {"scope": "opportunity_reassessment", "market_type": "SPOT",
         "venue": "BINANCE_SPOT_TESTNET"},
        "spot-reassessment-1",
    )

    assert seen["limit"] == 20
    assert seen["tick"].symbol == "BTCUSDT"
    assert seen["tick"].price == 123.4
    assert seen["tick"].candle_close == 123.1
    assert seen["candle_request"] == ("BTCUSDT", "1h", 2)


def test_opportunity_reassessment_handler_rejects_cross_venue_job_payload():
    from backend.opportunity_coordinator import OpportunityCoordinator
    from backend.shared_market_worker import opportunity_reassessment_handler

    coordinator = object.__new__(OpportunityCoordinator)
    coordinator.market_type = "SPOT"
    coordinator.venue = "BINANCE_SPOT_TESTNET"
    coordinator.reassess_due = lambda **kwargs: None

    class SpotSource:
        def fetch_mark_price(self, symbol):
            raise AssertionError("cross-venue payload must be rejected before fetching")

    handler = opportunity_reassessment_handler(coordinator, SpotSource())
    with pytest.raises(ValueError, match="venue"):
        handler(
            {"scope": "opportunity_reassessment", "market_type": "FUTURES",
             "venue": "BINANCE_USDM_TESTNET"},
            "wrong-venue-job",
        )



def test_futures_reassessment_handler_hands_off_triggers_and_reconciles_ledger(monkeypatch):
    from types import SimpleNamespace
    from datetime import datetime, timezone

    from backend.opportunity_coordinator import OpportunityCoordinator
    from backend.shared_market_worker import opportunity_reassessment_handler

    record = SimpleNamespace(opportunity_id="opp-triggered", state="TRIGGERED")
    seen = {"handoff": [], "reconcile": 0, "states": None}

    class Store:
        def list_opportunities(self, **kwargs):
            seen["states"] = kwargs["states"]
            return SimpleNamespace(items=(record,), next_cursor=None)

    coordinator = object.__new__(OpportunityCoordinator)
    coordinator.market_type = "FUTURES"
    coordinator.venue = "BINANCE_USDM_TESTNET"
    coordinator.store = Store()
    coordinator.reassess_due = lambda **kwargs: SimpleNamespace(
        evaluated_count=1, failed_count=0, orders_authorized=False,
    )

    def process(engine, opportunity, *, source, paper_service, now):
        seen["handoff"].append((opportunity.opportunity_id, paper_service))
        return {"status": "INTENT_RECORDED", "orders_authorized": False}

    def reconcile(engine, ledger, *, now):
        seen["reconcile"] += 1
        return ({"state": "INTENT_RECORDED", "orders_authorized": False},)

    monkeypatch.setattr(
        "backend.opportunity_handoff.process_triggered_opportunity", process,
    )
    monkeypatch.setattr(
        "backend.opportunity_handoff.reconcile_paper_intents", reconcile,
    )

    class FuturesSource:
        def fetch_mark_price(self, symbol):
            return SimpleNamespace(
                symbol=symbol, mark_price=150.0,
                observed_at=datetime.now(timezone.utc),
                orders_authorized=False, market_type="FUTURES",
            )

    paper_service = SimpleNamespace(ledger=object())
    handler = opportunity_reassessment_handler(
        coordinator, FuturesSource(), paper_service=paper_service,
        allow_paper_handoff=True,
    )
    handler(
        {"scope": "opportunity_reassessment", "market_type": "FUTURES",
         "venue": "BINANCE_USDM_TESTNET"},
        "futures-reassessment-1",
    )

    assert seen["states"] == ("TRIGGERED", "RISK_CHECK")
    assert seen["handoff"] == [("opp-triggered", paper_service)]
    assert seen["reconcile"] == 1




def test_research_activation_profile_uses_conservative_bounded_defaults(monkeypatch):
    for name in (
        "PAPER_MARKET_INTELLIGENCE_INTERVAL_SECONDS",
        "PAPER_OPPORTUNITY_REASSESS_INTERVAL_SECONDS",
        "PAPER_MARKET_INTELLIGENCE_CANDLE_LIMIT",
        "PAPER_MARKET_INTELLIGENCE_FETCH_WORKERS",
        "PAPER_SCHEDULED_JOB_POLL_INTERVAL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    assert shared_market_worker._bounded_env_int(
        "PAPER_MARKET_INTELLIGENCE_INTERVAL_SECONDS", 1800,
        minimum=900, maximum=86400,
    ) == 1800
    assert shared_market_worker._bounded_env_int(
        "PAPER_OPPORTUNITY_REASSESS_INTERVAL_SECONDS", 300,
        minimum=300, maximum=3600,
    ) == 300
    assert shared_market_worker._bounded_env_int(
        "PAPER_MARKET_INTELLIGENCE_CANDLE_LIMIT", 100,
        minimum=30, maximum=500,
    ) == 100
    assert shared_market_worker._bounded_env_int(
        "PAPER_MARKET_INTELLIGENCE_FETCH_WORKERS", 3,
        minimum=1, maximum=6,
    ) == 3
    assert shared_market_worker._bounded_env_int(
        "PAPER_SCHEDULED_JOB_POLL_INTERVAL_SECONDS", 5,
        minimum=3, maximum=30,
    ) == 5


@pytest.mark.parametrize(
    ("name", "value", "minimum", "maximum"),
    [
        ("PAPER_MARKET_INTELLIGENCE_INTERVAL_SECONDS", "899", 900, 86400),
        ("PAPER_OPPORTUNITY_REASSESS_INTERVAL_SECONDS", "299", 300, 3600),
        ("PAPER_MARKET_INTELLIGENCE_CANDLE_LIMIT", "501", 30, 500),
        ("PAPER_MARKET_INTELLIGENCE_FETCH_WORKERS", "7", 1, 6),
        ("PAPER_MARKET_INTELLIGENCE_FETCH_WORKERS", "not-a-number", 1, 6),
        ("PAPER_SCHEDULED_JOB_POLL_INTERVAL_SECONDS", "2", 3, 30),
    ],
)
def test_research_activation_profile_rejects_unsafe_or_invalid_overrides(
    monkeypatch, name, value, minimum, maximum,
):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        shared_market_worker._bounded_env_int(
            name, 1, minimum=minimum, maximum=maximum,
        )


def test_opportunity_reassessment_batch_is_bounded_to_twenty_records():
    from backend.shared_market_worker import opportunity_reassessment_handler
    from backend.opportunity_coordinator import OpportunityCoordinator
    from types import SimpleNamespace

    coordinator = object.__new__(OpportunityCoordinator)
    coordinator.market_type = "SPOT"
    coordinator.venue = "BINANCE_SPOT_TESTNET"
    seen = {}

    def reassess_due(*, now, market_update_provider, limit):
        seen["limit"] = limit
        return SimpleNamespace(evaluated_count=0, failed_count=0, orders_authorized=False)

    coordinator.reassess_due = reassess_due

    class SpotSource:
        def fetch_mark_price(self, symbol):
            raise AssertionError("empty batch must not fetch prices")

    handler = opportunity_reassessment_handler(coordinator, SpotSource())
    handler(
        {"scope": "opportunity_reassessment", "market_type": "SPOT",
         "venue": "BINANCE_SPOT_TESTNET"},
        "bounded-spot-reassessment",
    )
    assert seen["limit"] == 20


def test_market_intelligence_worker_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("PAPER_MARKET_INTELLIGENCE_ENABLED", raising=False)
    assert shared_market_worker._env_bool("PAPER_MARKET_INTELLIGENCE_ENABLED") is False


def test_paper_opportunity_handoff_is_disabled_by_default(monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from backend.opportunity_coordinator import OpportunityCoordinator
    from backend.shared_market_worker import opportunity_reassessment_handler

    coordinator = object.__new__(OpportunityCoordinator)
    coordinator.market_type = "FUTURES"
    coordinator.venue = "BINANCE_USDM_TESTNET"
    coordinator.store = SimpleNamespace(
        list_opportunities=lambda **kwargs: SimpleNamespace(
            items=(SimpleNamespace(opportunity_id="opp-triggered", state="TRIGGERED"),),
            next_cursor=None,
        )
    )
    coordinator.reassess_due = lambda **kwargs: SimpleNamespace(
        evaluated_count=0, failed_count=0, orders_authorized=False,
    )

    def unexpected_handoff(*args, **kwargs):
        raise AssertionError("paper handoff must remain disabled by default")

    monkeypatch.setattr(
        "backend.opportunity_handoff.process_triggered_opportunity", unexpected_handoff,
    )
    monkeypatch.setattr(
        "backend.opportunity_handoff.reconcile_paper_intents",
        lambda *args, **kwargs: (),
    )

    class FuturesSource:
        def fetch_mark_price(self, symbol):
            raise AssertionError("no market fetch is needed for an empty reassessment")

    handler = opportunity_reassessment_handler(
        coordinator, FuturesSource(), paper_service=SimpleNamespace(ledger=object()),
    )
    handler(
        {"scope": "opportunity_reassessment", "market_type": "FUTURES",
         "venue": "BINANCE_USDM_TESTNET"},
        "futures-handoff-default-off",
    )


def test_env_bool_accepts_explicit_opt_in_and_rejects_invalid_values(monkeypatch):
    monkeypatch.setenv("PAPER_MARKET_INTELLIGENCE_ENABLED", "true")
    assert shared_market_worker._env_bool("PAPER_MARKET_INTELLIGENCE_ENABLED") is True
    monkeypatch.setenv("PAPER_MARKET_INTELLIGENCE_ENABLED", "sometimes")
    with pytest.raises(ValueError, match="must be a boolean"):
        shared_market_worker._env_bool("PAPER_MARKET_INTELLIGENCE_ENABLED")
