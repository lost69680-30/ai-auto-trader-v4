from __future__ import annotations

from backend.staging_preflight import check_database_schema, run_preflight


def worker_env(**overrides):
    values = {
        "PAPER_DATABASE_URL": "postgresql://paper:secret@db.example.test:5432/solo",
        "PAPER_MARKET_TESTNET": "true",
        "PAPER_CANDLE_INTERVAL": "1m",
        "PAPER_MARKET_INTELLIGENCE_ENABLED": "false",
        "PAPER_OPPORTUNITY_PAPER_HANDOFF_ENABLED": "false",
        "PAPER_RESEARCH_WORKER_ENABLED": "false",
        "PAPER_MARKET_SUPERVISOR_ENABLED": "false",
        "PAPER_SYMBOLS": "BTCUSDT,ETHUSDT",
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
    values.update(overrides)
    return values


def api_env(**overrides):
    values = worker_env(
        PAPER_MARKET_ROLE="api",
        PAPER_MARKET_DATA_MODE="futures",
        PAPER_API_TOKEN="staging-only-placeholder-token-0123456789",
        PAPER_API_HOST="127.0.0.1",
        PAPER_WEBSOCKET_ENABLED="true",
    )
    values.update(overrides)
    return values


def test_worker_preflight_accepts_separate_testnet_worker_configuration():
    report = run_preflight("worker", worker_env())

    assert report.passed is True
    assert report.orders_authorized is False
    assert all(check.passed for check in report.checks)


def test_worker_preflight_rejects_production_market_data_and_opt_in_features():
    report = run_preflight("worker", worker_env(
        PAPER_MARKET_TESTNET="false",
        PAPER_MARKET_INTELLIGENCE_ENABLED="true",
        PAPER_OPPORTUNITY_PAPER_HANDOFF_ENABLED="true",
    ))

    assert report.passed is False
    failed = {check.name for check in report.checks if not check.passed}
    assert {"testnet_required", "market_intelligence_disabled", "paper_handoff_disabled"} <= failed


def test_api_preflight_requires_websocket_strong_token_and_tls_termination():
    report = run_preflight("api", api_env(
        PAPER_WEBSOCKET_ENABLED="false",
        PAPER_API_TOKEN="short",
        PAPER_API_HOST="0.0.0.0",
    ))

    assert report.passed is False
    failed = {check.name for check in report.checks if not check.passed}
    assert {"websocket_enabled", "api_token_strength", "https_termination"} <= failed


def test_api_preflight_requires_api_only_role_and_futures_testnet():
    report = run_preflight("api", api_env(
        PAPER_MARKET_ROLE="combined",
        PAPER_MARKET_DATA_MODE="spot",
        PAPER_MARKET_TESTNET="false",
        PAPER_RESEARCH_WORKER_ENABLED="true",
    ))

    assert report.passed is False
    failed = {check.name for check in report.checks if not check.passed}
    assert {"api_role", "futures_market_mode", "testnet_required", "research_worker_disabled"} <= failed


def test_preflight_does_not_leak_database_credentials_in_diagnostics():
    report = run_preflight("worker", worker_env(PAPER_DATABASE_URL="postgresql://alice:super-secret@db.example.test/solo"))

    rendered = " ".join(check.detail for check in report.checks)
    assert "super-secret" not in rendered
    assert "alice" not in rendered


def test_database_schema_check_is_read_only_and_requires_all_private_tables():
    expected_tables = {
        "paper_orders", "paper_fills", "paper_positions", "paper_funding_events",
        "paper_audit_events", "paper_system_controls", "paper_order_cancellations",
        "paper_position_extrema", "paper_equity_snapshots", "market_feed_snapshots",
        "market_worker_heartbeats", "scheduled_job_definitions", "scheduled_jobs",
        "market_candles", "market_intelligence_reports", "opportunities",
        "opportunity_assessments", "opportunity_evidence",
        "opportunity_watch_conditions", "opportunity_events", "opportunity_claims",
    }

    class Result:
        def __init__(self, rows=()):
            self._rows = list(rows)

        def fetchall(self):
            return self._rows

    class FakeConnection:
        def __init__(self):
            self.statements = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def transaction(self):
            return self

        def execute(self, statement, params=None):
            self.statements.append((statement, params))
            if statement == "SET TRANSACTION READ ONLY":
                return Result()
            return Result([(name, True) for name in sorted(expected_tables)])

    connection = FakeConnection()
    calls = []

    def connect(dsn, **kwargs):
        calls.append((dsn, kwargs))
        return connection

    check = check_database_schema(
        "postgresql://paper:secret@127.0.0.1:5432/solo",
        connect_factory=connect,
    )

    assert check.passed is True
    assert calls and calls[0][0].startswith("postgresql://")
    assert connection.statements[0][0] == "SET TRANSACTION READ ONLY"
    assert len(connection.statements) == 2
    assert "super-secret" not in check.detail
    assert "read-only" in check.detail.lower()


def test_database_schema_check_fails_when_required_table_or_rls_is_missing():
    class Result:
        def fetchall(self):
            return [("paper_orders", True), ("paper_fills", False)]

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def transaction(self):
            return self

        def execute(self, statement, params=None):
            return Result()

    check = check_database_schema(
        "postgresql://paper:secret@127.0.0.1:5432/solo",
        connect_factory=lambda *args, **kwargs: FakeConnection(),
    )

    assert check.passed is False
    assert "missing" in check.detail.lower()
    assert "row-level security" in check.detail.lower()

def test_remote_database_preflight_rejects_connection_without_tls():
    class Result:
        def fetchall(self):
            return [(name, True) for name in sorted({
                "paper_orders", "paper_fills", "paper_positions", "paper_funding_events",
                "paper_audit_events", "paper_system_controls", "paper_order_cancellations",
                "paper_position_extrema", "paper_equity_snapshots", "market_feed_snapshots",
                "market_worker_heartbeats", "scheduled_job_definitions", "scheduled_jobs",
                "market_candles", "market_intelligence_reports", "opportunities",
                "opportunity_assessments", "opportunity_evidence",
                "opportunity_watch_conditions", "opportunity_events", "opportunity_claims",
            })]

    class Info:
        ssl_in_use = False

    class FakeConnection:
        info = Info()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def transaction(self):
            return self

        def execute(self, statement, params=None):
            return Result()

    check = check_database_schema(
        "postgresql://paper:secret@db.example.test:5432/solo",
        connect_factory=lambda *args, **kwargs: FakeConnection(),
    )

    assert check.passed is False
    assert "TLS" in check.detail

def test_worker_preflight_rejects_runtime_risk_limits_above_safety_caps():
    report = run_preflight("worker", worker_env(
        PAPER_MAX_DAILY_LOSS="21",
        PAPER_MAX_POSITION_NOTIONAL="101",
    ))

    assert report.passed is False
    runtime_check = next(check for check in report.checks if check.name == "paper_runtime_configuration")
    assert runtime_check.passed is False
    assert "risk limits" in runtime_check.detail.lower()


def test_worker_preflight_rejects_missing_required_paper_runtime_settings():
    values = worker_env()
    values.pop("PAPER_SYMBOLS")
    report = run_preflight("worker", values)

    runtime_check = next(check for check in report.checks if check.name == "paper_runtime_configuration")
    assert runtime_check.passed is False
    assert "PAPER_*" in runtime_check.detail


def test_api_preflight_rejects_market_allowlist_or_archive_interval_mismatch():
    report = run_preflight("api", api_env(
        PAPER_MARKET_SYMBOLS="BTCUSDT",
        PAPER_MARKET_INTERVAL="5m",
    ))

    assert report.passed is False
    failed = {check.name for check in report.checks if not check.passed}
    assert {"market_symbol_allowlist_match", "market_archive_interval_match"} <= failed
