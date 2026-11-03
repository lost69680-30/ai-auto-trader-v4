"""End-to-end paper service recovery against a disposable PostgreSQL ledger."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import pytest

from app.core.models import RiskLimits
from app.engines.paper_service_app import (
    PaperServiceApplication, PaperServiceConfig, PaperSymbolPolicy,
)
from app.engines.orchestrator import PaperOrderIntent
from backend.market_data_adapter import MarketCandle, MarketUniverse, MarkPriceSnapshot
from backend.market_worker import FeedStatus, MarketFrame, MarketPollResult
from backend.scanner import Contract, MarketSnapshot

TEST_DSN = os.environ.get("PAPER_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="PAPER_TEST_DATABASE_URL is not configured")
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def disposable_postgres_schema():
    host = (urlparse(TEST_DSN).hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "::1", "postgres"}:
        pytest.fail("Refusing to reset paper_ledger outside a local disposable PostgreSQL database.")
    migration_name = "20261029205318_create_paper_ledger_schema.sql"
    repo_root = next(
        (parent for parent in Path(__file__).resolve().parents
         if (parent / "supabase" / "migrations" / migration_name).is_file()),
        None,
    )
    if repo_root is None:
        pytest.fail(f"Required migration not found: {migration_name}")
    migration = repo_root / "supabase" / "migrations" / migration_name
    with psycopg.connect(TEST_DSN, autocommit=True, prepare_threshold=None) as connection:
        connection.execute("DROP SCHEMA IF EXISTS paper_ledger CASCADE")
        for role in ("anon", "authenticated", "service_role"):
            if connection.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
            ).fetchone() is None:
                connection.execute(f"CREATE ROLE {role} NOLOGIN")
        connection.execute(migration.read_text(encoding="utf-8"), prepare=False)
    yield


class FakeSource:
    def __init__(self):
        self.current_price = 100.0
        contract = Contract(
            "BTCUSDT", "TRADING", "PERPETUAL", "USDT",
            tick_size=0.1, quantity_step=0.001, min_quantity=0.001,
        )
        self.universe = MarketUniverse(
            contracts=(contract,),
            snapshots=(MarketSnapshot("BTCUSDT", NOW, 100.0, 5_000_000.0, 1.0, 2.0),),
            fetched_at=NOW,
            orders_authorized=False,
        )

    def fetch_market_universe(self, *, now=None):
        stamp = now or NOW
        return replace(
            self.universe,
            fetched_at=stamp,
            snapshots=tuple(
                replace(snapshot, observed_at=stamp, last_price=self.current_price)
                for snapshot in self.universe.snapshots
            ),
        )

    def fetch_mark_price(self, symbol):
        return MarkPriceSnapshot(
            symbol, self.current_price, self.current_price, 0.0001,
            NOW + timedelta(hours=8), NOW, orders_authorized=False,
        )

    def fetch_funding_rate_history(self, symbol, *, start_time_ms=None, end_time_ms=None, limit=1000):
        return ()

    def fetch_klines(self, symbol, *, interval="1m", limit=100, now=None):
        stamp = now or NOW
        rows = []
        base_open = stamp - timedelta(seconds=60 * 40)
        for index in range(40):
            opened = base_open + timedelta(seconds=60 * index)
            closed = opened + timedelta(seconds=59)
            price = 101.0 if index == 39 else 80 + index * 0.5
            volume = 1000.0 if index == 39 else 100.0 + index
            rows.append(MarketCandle(
                symbol, opened, closed, price - 0.1, price + 0.4,
                price - 0.2, price, volume, volume * price, 20, True,
            ))
        return tuple(rows)


def _config(tmp_path):
    return PaperServiceConfig(
        database_path=str(tmp_path / "unused-local-ledger.sqlite"),
        symbols=(PaperSymbolPolicy("BTCUSDT", estimated_round_trip_cost_pct=0.12),),
        simulated_equity=1000.0,
        risk_fraction=0.005,
        limits=RiskLimits(
            max_position_notional=100.0, max_daily_loss=20.0,
            max_open_positions=2, max_risk_per_trade=5.0,
        ),
        fee_rate=0.0004,
        slippage_bps=2.0,
    )


def _frame(now, price, candles):
    return MarketFrame(
        "BTCUSDT", FeedStatus.HEALTHY, now, price, now - timedelta(seconds=1),
        candles, (), 0, orders_authorized=False, funding_rate=0.0001,
    )


def test_postgres_paper_service_pipeline_emergency_stop_and_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_DATABASE_URL", TEST_DSN)
    source = FakeSource()
    config = _config(tmp_path)
    current_clock = [NOW]
    app = PaperServiceApplication.create(
        source=source, config=config, clock=lambda: current_clock[0],
    )
    proposal_id = None
    try:
        first_candles = source.fetch_klines("BTCUSDT", now=NOW)
        first_price = first_candles[-1].close
        source.current_price = first_price
        first = app.process_poll(
            MarketPollResult(NOW, (_frame(NOW, first_price, first_candles),), orders_authorized=False),
            now=NOW.timestamp(),
        )
        assert first.orders_authorized is False
        assert first.analysis_result is not None
        assert first.analysis_result.research_result.candidates
        assert app.ledger.orders == ()

        next_at = NOW + timedelta(seconds=5)
        current_clock[0] = next_at
        source.current_price = first_price + 0.5
        # Keep the same closed-candle signal identity while advancing only the mark price.
        next_candles = first_candles
        second = app.process_poll(
            MarketPollResult(
                next_at, (_frame(next_at, source.current_price, next_candles),),
                orders_authorized=False,
            ),
            now=next_at.timestamp(),
        )
        assert second.orders_authorized is False
        assert second.analysis_result is not None
        assert second.analysis_result.pipeline_report is not None
        assert second.analysis_result.pipeline_report.items[0].status == "PAPER_ORDER_RECORDED"
        assert len(app.ledger.orders) == 1
        proposal_id = app.ledger.orders[0].proposal_id
        assert app.ledger.orders[0].status == "PENDING_PAPER_SIMULATION"
        assert app.ledger.orders_authorized is False

        stopped_at = next_at + timedelta(seconds=1)
        cancelled = app.activate_emergency_stop(
            reason="Part 8 disposable PostgreSQL recovery test", now=stopped_at,
        )
        assert cancelled == (proposal_id,)
        assert app.ledger.emergency_stop_active is True
        assert app.ledger.orders[0].status == "CANCELLED"
        assert app.ledger.verify_integrity() == "ok"
    finally:
        app.close()

    recovered_at = NOW + timedelta(seconds=10)
    current_clock[0] = recovered_at
    recovered = PaperServiceApplication.create(
        source=source, config=config, clock=lambda: current_clock[0],
    )
    try:
        assert recovered.ledger.emergency_stop_active is True
        assert recovered.ledger.orders_authorized is False
        assert any(
            order.proposal_id == proposal_id and order.status == "CANCELLED"
            for order in recovered.ledger.orders
        )
        assert any(
            event.proposal_id == proposal_id
            for event in recovered.ledger.audit_events
        )
        blocked = recovered.ledger.record_intent(PaperOrderIntent(
            proposal_id="part8-blocked-after-restart",
            signal_id="part8-blocked-after-restart",
            symbol="BTCUSDT", side="LONG", quantity=0.001,
            entry_price=100.0, stop_loss=98.0, take_profit=104.0,
            recorded_at=recovered_at.timestamp(),
        ))
        assert blocked == "BLOCKED_BY_EMERGENCY_STOP"
        assert recovered.ledger.verify_integrity() == "ok"
    finally:
        recovered.close()
