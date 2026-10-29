from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.core.models import RiskLimits
from app.engines.paper_service_app import PaperServiceApplication, PaperServiceConfig, PaperSymbolPolicy
from app.engines.paper_ledger import SQLitePaperLedger
from backend.market_data_adapter import MarketCandle, MarketUniverse, MarkPriceSnapshot
from backend.market_worker import FeedStatus, MarketFrame, MarketPollResult
from backend.scanner import Contract, MarketSnapshot

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


class FakeSource:
    def __init__(self, *, contract_metadata=True):
        self.current_price = 100.0
        contract = Contract(
            "BTCUSDT", "TRADING", "PERPETUAL", "USDT",
            tick_size=0.1 if contract_metadata else None,
            quantity_step=0.001 if contract_metadata else None,
            min_quantity=0.001 if contract_metadata else None,
        )
        self.universe = MarketUniverse(
            contracts=(contract,),
            snapshots=(MarketSnapshot("BTCUSDT", NOW, 100.0, 5_000_000.0, 1.0, 2.0),),
            fetched_at=NOW,
            orders_authorized=False,
        )

    def fetch_market_universe(self, *, now=None):
        return replace(self.universe, fetched_at=now or NOW, snapshots=tuple(
            replace(snapshot, observed_at=now or NOW, last_price=self.current_price)
            for snapshot in self.universe.snapshots
        ))

    def fetch_mark_price(self, symbol):
        return MarkPriceSnapshot(
            symbol, self.current_price, self.current_price, 0.0001, NOW + timedelta(hours=8), NOW,
            orders_authorized=False,
        )

    def fetch_funding_rate_history(self, symbol, *, start_time_ms=None, end_time_ms=None, limit=1000):
        return ()

    def fetch_klines(self, symbol, *, interval="1m", limit=100, now=None):
        return candles(symbol, now or NOW)


def candles(symbol, now):
    rows = []
    base_open = now - timedelta(seconds=60 * 40)
    for i in range(40):
        opened = base_open + timedelta(seconds=60 * i)
        closed = opened + timedelta(seconds=59)
        price = 101.0 if i == 39 else 80 + i * 0.5
        volume = 1000.0 if i == 39 else 100.0 + i
        rows.append(MarketCandle(
            symbol, opened, closed, price - 0.1, price + 0.4, price - 0.2, price,
            volume, volume * price, 20, True,
        ))
    return tuple(rows)


def good_frame(now=NOW):
    return MarketFrame(
        "BTCUSDT", FeedStatus.HEALTHY, now, 100.0, now - timedelta(seconds=1),
        candles("BTCUSDT", now), (), 0, orders_authorized=False, funding_rate=0.0001,
    )


def config(tmp_path):
    return PaperServiceConfig(
        database_path=str(tmp_path / "paper.sqlite"),
        symbols=(PaperSymbolPolicy("BTCUSDT", estimated_round_trip_cost_pct=0.12),),
        simulated_equity=1000.0,
        risk_fraction=0.005,
        limits=RiskLimits(max_position_notional=100.0, max_daily_loss=20.0,
                          max_open_positions=2, max_risk_per_trade=5.0),
        fee_rate=0.0004,
        slippage_bps=2.0,
    )


def test_service_composes_worker_poll_into_research_and_analysis_without_authorization(tmp_path):
    source = FakeSource()
    app = PaperServiceApplication.create(
        source=source, config=config(tmp_path), clock=lambda: NOW,
    )
    try:
        report = app.process_poll(MarketPollResult(NOW, (good_frame(),), orders_authorized=False), now=NOW.timestamp())

        assert report.orders_authorized is False
        assert report.analysis_result is not None
        assert report.analysis_result.orders_authorized is False
        assert report.analysis_result.research_result is not None
        assert report.analysis_result.research_result.orders_authorized is False
        assert report.analysis_result.pipeline_report is not None
        assert report.analysis_result.pipeline_report.orders_authorized is False
        assert report.healthy_symbols == ("BTCUSDT",)
    finally:
        app.close()


def test_service_rejects_malformed_snapshot_numeric_fields_per_symbol(tmp_path):
    class BadSnapshotSource(FakeSource):
        def fetch_market_universe(self, *, now=None):
            result = super().fetch_market_universe(now=now)
            return replace(result, snapshots=(replace(result.snapshots[0], last_price="not-a-price"),))

    app = PaperServiceApplication.create(
        source=BadSnapshotSource(), config=config(tmp_path), clock=lambda: NOW,
    )
    try:
        report = app.process_poll(MarketPollResult(NOW, (good_frame(),), orders_authorized=False), now=NOW.timestamp())
        assert report.orders_authorized is False
        assert report.analysis_result is not None
        assert report.analysis_result.status == "NO_ELIGIBLE_SYMBOLS"
        assert ("BTCUSDT", "market snapshot contains invalid numeric fields") in report.analysis_result.excluded_symbols
        assert app.ledger.orders == ()
    finally:
        app.close()


def test_service_fails_closed_when_contract_tick_or_quantity_metadata_is_missing(tmp_path):
    app = PaperServiceApplication.create(
        source=FakeSource(contract_metadata=False), config=config(tmp_path), clock=lambda: NOW,
    )
    try:
        report = app.process_poll(MarketPollResult(NOW, (good_frame(),), orders_authorized=False), now=NOW.timestamp())
        analysis = report.analysis_result
        assert analysis is not None
        assert analysis.orders_authorized is False
        assert all(item.status != "PAPER_ORDER_RECORDED" for item in (analysis.pipeline_report.items if analysis.pipeline_report else ()))
        assert app.ledger.orders == ()
    finally:
        app.close()


def test_service_config_rejects_duplicate_symbols_and_nonfinite_costs(tmp_path):
    base = config(tmp_path)
    with pytest.raises(ValueError, match="unique"):
        PaperServiceConfig(
            database_path=base.database_path,
            symbols=(PaperSymbolPolicy("BTCUSDT", 0.12), PaperSymbolPolicy("BTCUSDT", 0.12)),
            simulated_equity=1000.0, risk_fraction=0.005, limits=base.limits,
            fee_rate=0.0004, slippage_bps=2.0,
        )
    with pytest.raises(ValueError, match="finite"):
        PaperSymbolPolicy("BTCUSDT", float("nan"))


def test_from_env_requires_explicit_equity_risk_limits_and_cost_assumptions():
    with pytest.raises(ValueError, match="missing required paper configuration"):
        PaperServiceConfig.from_env({})

    env = {
        "PAPER_SYMBOLS": "BTCUSDT",
        "PAPER_SIMULATED_EQUITY": "1000",
        "PAPER_RISK_FRACTION": "0.005",
        "PAPER_MAX_POSITION_NOTIONAL": "100",
        "PAPER_MAX_DAILY_LOSS": "20",
        "PAPER_MAX_OPEN_POSITIONS": "2",
        "PAPER_MAX_RISK_PER_TRADE": "5",
        "PAPER_ESTIMATED_ROUND_TRIP_COST_PCT": "0.12",
        "PAPER_FEE_RATE": "0.0004",
        "PAPER_SLIPPAGE_BPS": "2",
    }
    parsed = PaperServiceConfig.from_env(env)
    assert parsed.simulated_equity == 1000
    assert parsed.limits.max_daily_loss == 20
    assert parsed.max_gross_exposure == 1000
    assert parsed.plan_lifetime_seconds == 20

    # Environment selection is security-sensitive even though the source is read-only:
    # a typo must not silently change which venue is contacted.
    from app.engines.paper_service_app import _parse_market_testnet
    with pytest.raises(ValueError, match="PAPER_MARKET_TESTNET"):
        _parse_market_testnet("truue")
    assert _parse_market_testnet("true") is True
    assert _parse_market_testnet("false") is False


def test_service_rejects_gross_exposure_above_equity_and_signal_lifetime_gap(tmp_path):
    base = config(tmp_path)
    with pytest.raises(ValueError, match="no greater than simulated equity"):
        PaperServiceConfig(
            database_path=base.database_path, symbols=base.symbols, simulated_equity=1000,
            risk_fraction=0.005, limits=base.limits, fee_rate=0.0004, slippage_bps=2,
            max_gross_exposure=1001,
        )
    with pytest.raises(ValueError, match="freshness window"):
        PaperServiceConfig(
            database_path=base.database_path, symbols=base.symbols, simulated_equity=1000,
            risk_fraction=0.005, limits=base.limits, fee_rate=0.0004, slippage_bps=2,
            plan_lifetime_seconds=60,
        )


def test_emergency_stop_cancels_pending_paper_intents_and_does_not_authorize_orders(tmp_path):
    from app.engines.orchestrator import PaperOrderIntent

    app = PaperServiceApplication.create(source=FakeSource(), config=config(tmp_path), clock=lambda: NOW)
    intent = PaperOrderIntent(
        proposal_id="pending-1", signal_id="signal-1", symbol="BTCUSDT", side="LONG",
        quantity=0.1, entry_price=100, stop_loss=95, take_profit=110,
        recorded_at=NOW.timestamp() - 1,
    )
    try:
        assert app.ledger.record_intent(intent) == "RECORDED"
        cancelled = app.activate_emergency_stop(reason="operator test", now=NOW)
        assert cancelled == ("pending-1",)
        assert app.ledger.emergency_stop_active is True
        assert app.ledger.orders[0].status == "CANCELLED"
        assert app.ledger.orders_authorized is False
    finally:
        app.close()


def test_end_to_end_market_research_atr_trigger_risk_and_paper_ledger(tmp_path):
    source = FakeSource()
    current_clock = [NOW]
    app = PaperServiceApplication.create(
        source=source, config=config(tmp_path), clock=lambda: current_clock[0],
    )
    rows = candles("BTCUSDT", NOW)
    first_price = rows[-1].close
    source.current_price = first_price
    first_frame = MarketFrame(
        "BTCUSDT", FeedStatus.HEALTHY, NOW, first_price, NOW - timedelta(seconds=1),
        rows, (), 0, orders_authorized=False, funding_rate=0.0001,
    )
    try:
        first_report = app.process_poll(
            MarketPollResult(NOW, (first_frame,), orders_authorized=False), now=NOW.timestamp(),
        )
        assert first_report.analysis_result is not None
        assert first_report.analysis_result.research_result is not None
        candidate_rows = first_report.analysis_result.research_result.candidates
        assert candidate_rows and candidate_rows[0].status == "REVIEW"
        assert app.ledger.orders == ()  # First reference mark is below the trigger.

        next_now = NOW + timedelta(seconds=5)
        current_clock[0] = next_now
        source.current_price = first_price + 0.5
        next_frame = MarketFrame(
            "BTCUSDT", FeedStatus.HEALTHY, next_now, source.current_price,
            next_now - timedelta(seconds=1), rows, (), 0,
            orders_authorized=False, funding_rate=0.0001,
        )
        second_report = app.process_poll(
            MarketPollResult(next_now, (next_frame,), orders_authorized=False), now=next_now.timestamp(),
        )

        assert second_report.orders_authorized is False
        assert second_report.analysis_result is not None
        assert second_report.analysis_result.pipeline_report is not None
        assert second_report.analysis_result.pipeline_report.items[0].status == "PAPER_ORDER_RECORDED"
        assert len(app.ledger.orders) == 1
        order = app.ledger.orders[0]
        assert order.status == "PENDING_PAPER_SIMULATION"
        assert order.symbol == "BTCUSDT"
        assert order.quantity * order.entry_price <= config(tmp_path).limits.max_position_notional
        assert app.ledger.orders_authorized is False
    finally:
        app.close()


def test_service_startup_cancels_pending_intents_from_prior_process(tmp_path):
    path = tmp_path / "paper.sqlite"
    old_ledger = SQLitePaperLedger(path)
    from app.engines.orchestrator import PaperOrderIntent
    assert old_ledger.record_intent(PaperOrderIntent(
        proposal_id="stale-proposal", signal_id="old-signal", symbol="BTCUSDT",
        side="LONG", quantity=0.01, entry_price=100.0, stop_loss=98.0,
        take_profit=104.0, recorded_at=NOW.timestamp(),
    )) == "RECORDED"
    old_ledger.close()

    app = PaperServiceApplication.create(
        source=FakeSource(), config=config(tmp_path), clock=lambda: NOW,
    )
    try:
        orders = {order.proposal_id: order for order in app.ledger.orders}
        assert orders["stale-proposal"].status == "CANCELLED"
        assert any(
            event.event_type == "PAPER_ORDER_CANCELLED_ON_SERVICE_START"
            and event.proposal_id == "stale-proposal"
            for event in app.ledger.audit_events
        )
        assert app.ledger.orders_authorized is False
    finally:
        app.close()



def test_service_stale_first_market_frame_fails_closed_end_to_end(tmp_path):
    class StaleFirstMarkSource(FakeSource):
        def fetch_mark_price(self, symbol):
            return MarkPriceSnapshot(
                symbol, self.current_price, self.current_price, 0.0001,
                NOW + timedelta(hours=8), NOW - timedelta(minutes=3),
                orders_authorized=False,
            )

    app = PaperServiceApplication.create(
        source=StaleFirstMarkSource(),
        config=config(tmp_path),
        clock=lambda: NOW,
    )
    try:
        first_poll = app.worker.poll_once(now=NOW)
        assert first_poll.orders_authorized is False
        assert len(first_poll.frames) == 1
        assert first_poll.frames[0].status.value == "STALE"
        assert first_poll.frames[0].closed_candles == ()

        report = app.process_poll(first_poll, now=NOW.timestamp())

        assert report.analysis_status == "SKIPPED_UNHEALTHY_MARKET_DATA"
        assert report.analysis_result is None
        assert report.healthy_symbols == ()
        assert report.unhealthy_symbols == ("BTCUSDT",)
        assert report.fill_statuses == ()
        assert report.open_positions == 0
        assert app.ledger.orders == ()
        assert app.ledger.fills == ()
        assert app.ledger.orders_authorized is False
    finally:
        app.close()


def test_service_config_rejects_daily_loss_and_position_caps_above_policy(tmp_path):
    safe = PaperServiceConfig(
        database_path=str(tmp_path / "safe.sqlite"),
        symbols=(PaperSymbolPolicy("BTCUSDT", 0.12),),
        simulated_equity=1000.0,
        risk_fraction=0.005,
        limits=RiskLimits(max_position_notional=100.0, max_daily_loss=20.0,
                          max_open_positions=2, max_risk_per_trade=5.0),
        fee_rate=0.0004,
        slippage_bps=2.0,
    )
    with pytest.raises(ValueError, match="2 percent"):
        PaperServiceConfig(
            database_path=str(tmp_path / "daily-over.sqlite"),
            symbols=safe.symbols,
            simulated_equity=1000.0,
            risk_fraction=0.005,
            limits=RiskLimits(max_position_notional=100.0, max_daily_loss=20.01,
                              max_open_positions=2, max_risk_per_trade=5.0),
            fee_rate=0.0004,
            slippage_bps=2.0,
        )
    with pytest.raises(ValueError, match="10 percent"):
        PaperServiceConfig(
            database_path=str(tmp_path / "asset-over.sqlite"),
            symbols=safe.symbols,
            simulated_equity=1000.0,
            risk_fraction=0.005,
            limits=RiskLimits(max_position_notional=100.01, max_daily_loss=20.0,
                              max_open_positions=2, max_risk_per_trade=5.0),
            fee_rate=0.0004,
            slippage_bps=2.0,
        )



def test_service_uses_postgres_ledger_when_database_url_is_configured(tmp_path, monkeypatch):
    import app.engines.paper_service_app as service_module

    sqlite_for_test = tmp_path / "fake-postgres-ledger.sqlite"
    ignored_sqlite_path = tmp_path / "must-not-create" / "paper.sqlite"
    monkeypatch.setenv("PAPER_DATABASE_URL", "postgresql://test.invalid/paper")
    monkeypatch.setattr(
        service_module, "PostgresPaperLedger",
        lambda dsn: SQLitePaperLedger(sqlite_for_test),
        raising=False,
    )
    cfg = replace(config(tmp_path), database_path=str(ignored_sqlite_path))

    app = PaperServiceApplication.create(
        source=FakeSource(), config=cfg, clock=lambda: NOW,
    )
    try:
        assert app.ledger.database_path == str(sqlite_for_test)
        assert not ignored_sqlite_path.parent.exists()
    finally:
        app.close()


def test_service_fails_closed_when_configured_postgres_url_is_empty(tmp_path, monkeypatch):
    import app.engines.paper_service_app as service_module

    ignored_sqlite_path = tmp_path / "must-not-create" / "paper.sqlite"
    monkeypatch.setenv("PAPER_DATABASE_URL", " ")
    cfg = replace(config(tmp_path), database_path=str(ignored_sqlite_path))

    with pytest.raises(RuntimeError, match="PAPER_DATABASE_URL"):
        PaperServiceApplication.create(
            source=FakeSource(), config=cfg, clock=lambda: NOW,
        )
    assert not ignored_sqlite_path.parent.exists()


def test_service_does_not_fall_back_to_sqlite_when_postgres_initialization_fails(tmp_path, monkeypatch):
    import app.engines.paper_service_app as service_module

    ignored_sqlite_path = tmp_path / "must-not-create" / "paper.sqlite"
    monkeypatch.setenv("PAPER_DATABASE_URL", "postgresql://test.invalid/paper")

    def fail_postgres(_dsn):
        raise RuntimeError("test database unavailable")

    monkeypatch.setattr(service_module, "PostgresPaperLedger", fail_postgres, raising=False)
    cfg = replace(config(tmp_path), database_path=str(ignored_sqlite_path))

    with pytest.raises(RuntimeError, match="test database unavailable"):
        PaperServiceApplication.create(
            source=FakeSource(), config=cfg, clock=lambda: NOW,
        )
    assert not ignored_sqlite_path.parent.exists()


def test_funding_sync_records_verified_event_for_open_paper_position(tmp_path, monkeypatch):
    from app.engines.orchestrator import PaperOrderIntent
    from backend.market_data_adapter import FundingRateRecord

    class FundingSource(FakeSource):
        def fetch_funding_rate_history(self, symbol, *, start_time_ms=None, end_time_ms=None, limit=1000):
            assert symbol == "BTCUSDT"
            return (FundingRateRecord(
                "BTCUSDT", 0.001, NOW - timedelta(hours=1), mark_price=100.0,
                orders_authorized=False,
            ),)

    monkeypatch.delenv("PAPER_DATABASE_URL", raising=False)
    app = PaperServiceApplication.create(source=FundingSource(), config=config(tmp_path), clock=lambda: NOW)
    intent = PaperOrderIntent(
        proposal_id="funding-long-1", signal_id="funding-signal-1", symbol="BTCUSDT",
        side="LONG", quantity=1.0, entry_price=100.0, stop_loss=90.0,
        take_profit=110.0, recorded_at=(NOW - timedelta(hours=3)).timestamp(),
    )
    try:
        assert app.ledger.record_intent(intent) == "RECORDED"
        fill = app.ledger.simulate_fill(
            intent.proposal_id, market_price=100.0,
            filled_at=(NOW - timedelta(hours=2)).timestamp(), fee_rate=0.0, slippage_bps=0.0,
        )
        assert fill.status == "FILLED"

        report = app.runtime._analysis_handler._sync_funding_events(now=NOW)

        assert report.status == "OK"
        assert report.events_recorded == 1
        assert len(app.ledger.funding_events) == 1
        assert app.ledger.positions[0].funding_pnl == pytest.approx(-0.1)
        assert app.ledger.funding_events[0]["mark_price"] == 100.0
    finally:
        app.close()


def test_funding_sync_does_not_invent_missing_historical_mark_price(tmp_path, monkeypatch):
    from app.engines.orchestrator import PaperOrderIntent
    from backend.market_data_adapter import FundingRateRecord

    class MissingMarkSource(FakeSource):
        def fetch_funding_rate_history(self, symbol, *, start_time_ms=None, end_time_ms=None, limit=1000):
            return (
                FundingRateRecord(
                    "BTCUSDT", 0.001, NOW - timedelta(hours=1), mark_price=None,
                    orders_authorized=False,
                ),
                FundingRateRecord(
                    "BTCUSDT", 0.002, NOW - timedelta(minutes=30), mark_price=100.0,
                    orders_authorized=False,
                ),
            )

    monkeypatch.delenv("PAPER_DATABASE_URL", raising=False)
    app = PaperServiceApplication.create(source=MissingMarkSource(), config=config(tmp_path), clock=lambda: NOW)
    intent = PaperOrderIntent(
        proposal_id="funding-long-2", signal_id="funding-signal-2", symbol="BTCUSDT",
        side="LONG", quantity=1.0, entry_price=100.0, stop_loss=90.0,
        take_profit=110.0, recorded_at=(NOW - timedelta(hours=3)).timestamp(),
    )
    try:
        assert app.ledger.record_intent(intent) == "RECORDED"
        fill = app.ledger.simulate_fill(
            intent.proposal_id, market_price=100.0,
            filled_at=(NOW - timedelta(hours=2)).timestamp(), fee_rate=0.0, slippage_bps=0.0,
        )
        assert fill.status == "FILLED"

        report = app.runtime._analysis_handler._sync_funding_events(now=NOW)

        assert report.status == "DEGRADED"
        assert report.events_skipped_missing_mark == 1
        assert app.ledger.funding_events == ()
        assert app.ledger.positions[0].funding_pnl == 0.0
    finally:
        app.close()


def test_incomplete_funding_accounting_blocks_pending_paper_fills(tmp_path, monkeypatch):
    from app.engines.orchestrator import PaperOrderIntent
    from backend.market_data_adapter import FundingRateRecord

    class MissingMarkSource(FakeSource):
        def fetch_funding_rate_history(self, symbol, *, start_time_ms=None, end_time_ms=None, limit=1000):
            event = FundingRateRecord(
                "BTCUSDT", 0.001, NOW - timedelta(hours=1), mark_price=None,
                orders_authorized=False,
            )
            stamp_ms = int(event.funding_time.timestamp() * 1000)
            if start_time_ms is not None and stamp_ms < start_time_ms:
                return ()
            if end_time_ms is not None and stamp_ms > end_time_ms:
                return ()
            return (event,)

    monkeypatch.delenv("PAPER_DATABASE_URL", raising=False)
    app = PaperServiceApplication.create(source=MissingMarkSource(), config=config(tmp_path), clock=lambda: NOW)
    open_intent = PaperOrderIntent(
        proposal_id="funding-open-position", signal_id="funding-open-signal", symbol="BTCUSDT",
        side="LONG", quantity=1.0, entry_price=100.0, stop_loss=90.0,
        take_profit=110.0, recorded_at=(NOW - timedelta(hours=3)).timestamp(),
    )
    pending_intent = PaperOrderIntent(
        proposal_id="funding-pending-intent", signal_id="funding-pending-signal", symbol="BTCUSDT",
        side="LONG", quantity=0.1, entry_price=100.0, stop_loss=90.0,
        take_profit=110.0, recorded_at=(NOW - timedelta(seconds=30)).timestamp(),
    )
    try:
        assert app.ledger.record_intent(open_intent) == "RECORDED"
        fill = app.ledger.simulate_fill(
            open_intent.proposal_id, market_price=100.0,
            filled_at=(NOW - timedelta(hours=2)).timestamp(), fee_rate=0.0, slippage_bps=0.0,
        )
        assert fill.status == "FILLED"
        assert app.ledger.record_intent(pending_intent) == "RECORDED"

        report = app.process_poll(
            MarketPollResult(NOW, (good_frame(),), orders_authorized=False), now=NOW.timestamp(),
        )

        assert report.analysis_status == "FUNDING_SYNC_DEGRADED"
        assert report.orders_authorized is False
        assert report.fill_statuses == ()
        pending = next(order for order in app.ledger.orders if order.proposal_id == pending_intent.proposal_id)
        assert pending.status == "PENDING_PAPER_SIMULATION"
        assert len(app.ledger.fills) == 1
    finally:
        app.close()
