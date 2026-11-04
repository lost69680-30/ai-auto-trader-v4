"""Read-only deployment preflight for Solo's separated staging API and worker roles.

The default check validates configuration only. Database catalog inspection is
opt-in and runs inside a PostgreSQL READ ONLY transaction. This module never
applies migrations, writes application rows, or authorizes exchange orders.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from typing import Callable, Mapping


REQUIRED_PRIVATE_TABLES = frozenset({
    "paper_orders", "paper_fills", "paper_positions", "paper_funding_events",
    "paper_audit_events", "paper_system_controls", "paper_order_cancellations",
    "paper_position_extrema", "paper_equity_snapshots",
    "market_feed_snapshots", "market_worker_heartbeats",
    "scheduled_job_definitions", "scheduled_jobs", "market_candles",
    "market_intelligence_reports", "opportunities", "opportunity_assessments",
    "opportunity_evidence", "opportunity_watch_conditions", "opportunity_events",
    "opportunity_claims",
})
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off", ""})
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class _RemoteDatabaseTLSRequired(RuntimeError):
    """Remote PostgreSQL connections must negotiate TLS."""


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class PreflightReport:
    role: str
    checks: tuple[PreflightCheck, ...]
    orders_authorized: bool = False

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "passed": self.passed,
            "orders_authorized": False,
            "checks": [asdict(check) for check in self.checks],
        }


def _flag_is_false(environ: Mapping[str, str], name: str) -> bool:
    raw = environ.get(name, "false")
    return isinstance(raw, str) and raw.strip().lower() in _FALSE


def _flag_is_true(environ: Mapping[str, str], name: str) -> bool:
    raw = environ.get(name, "")
    return isinstance(raw, str) and raw.strip().lower() in _TRUE


def _database_dsn_check(environ: Mapping[str, str]) -> PreflightCheck:
    dsn = environ.get("PAPER_DATABASE_URL", "")
    if not isinstance(dsn, str) or not dsn.strip():
        return PreflightCheck("postgres_database_url", False, "PAPER_DATABASE_URL is required.")
    try:
        from psycopg.conninfo import conninfo_to_dict
        parsed = conninfo_to_dict(dsn.strip())
        hosts = [item.strip().lower() for item in parsed.get("host", "localhost").split(",") if item.strip()]
        if not hosts or not parsed.get("dbname"):
            raise ValueError("database name or host is missing")
        if parsed.get("hostaddr") and not hosts:
            raise ValueError("database host is missing")
    except Exception:
        return PreflightCheck(
            "postgres_database_url", False,
            "PAPER_DATABASE_URL must be a valid PostgreSQL connection URI or conninfo string; value redacted.",
        )
    return PreflightCheck(
        "postgres_database_url", True,
        "PostgreSQL connection string is present and parseable; credentials are not displayed.",
    )


def run_preflight(role: str, environ: Mapping[str, str] | None = None) -> PreflightReport:
    """Validate staging configuration without opening a network or database connection."""
    values = os.environ if environ is None else environ
    checks: list[PreflightCheck] = []
    if role not in {"worker", "api"}:
        return PreflightReport(role=str(role), checks=(
            PreflightCheck("role", False, "Role must be worker or api."),
        ))

    checks.append(_database_dsn_check(values))

    testnet = values.get("PAPER_MARKET_TESTNET")
    checks.append(PreflightCheck(
        "testnet_required",
        isinstance(testnet, str) and testnet.strip().lower() in _TRUE,
        "Binance Futures Testnet is explicitly selected."
        if isinstance(testnet, str) and testnet.strip().lower() in _TRUE
        else "PAPER_MARKET_TESTNET must be explicitly true; production market data is not accepted for staging.",
    ))
    candle_interval = values.get("PAPER_CANDLE_INTERVAL", "1m")
    checks.append(PreflightCheck(
        "one_minute_worker_candles", candle_interval == "1m",
        "Worker candle interval is 1m." if candle_interval == "1m"
        else "PAPER_CANDLE_INTERVAL must be 1m for the shared archive and worker.",
    ))
    if role == "worker":
        try:
            from app.engines.paper_service_app import PaperServiceConfig
            PaperServiceConfig.from_env(values)
            runtime_config_ok = True
        except Exception:
            runtime_config_ok = False
        checks.append(PreflightCheck(
            "paper_runtime_configuration", runtime_config_ok,
            "Required paper runtime settings and independent risk limits are valid."
            if runtime_config_ok
            else "Paper runtime settings or risk limits are invalid; verify required PAPER_* values and safety caps. Values are redacted.",
        ))

    for name, check_name, description in (
        ("PAPER_MARKET_INTELLIGENCE_ENABLED", "market_intelligence_disabled",
         "PAPER_MARKET_INTELLIGENCE_ENABLED"),
        ("PAPER_OPPORTUNITY_PAPER_HANDOFF_ENABLED", "paper_handoff_disabled",
         "PAPER_OPPORTUNITY_PAPER_HANDOFF_ENABLED"),
        ("PAPER_RESEARCH_WORKER_ENABLED", "research_worker_disabled",
         "PAPER_RESEARCH_WORKER_ENABLED"),
        ("PAPER_MARKET_SUPERVISOR_ENABLED", "supervisor_disabled",
         "PAPER_MARKET_SUPERVISOR_ENABLED"),
    ):
        is_false = _flag_is_false(values, name)
        checks.append(PreflightCheck(
            check_name, is_false,
            f"{description} is disabled." if is_false
            else f"{description} must be false or unset for this separated, paper-only staging profile.",
        ))

    if role == "api":
        api_role = values.get("PAPER_MARKET_ROLE", "").strip().lower() == "api"
        checks.append(PreflightCheck(
            "api_role", api_role,
            "API process is configured in API-only mode." if api_role
            else "PAPER_MARKET_ROLE must be api; do not start a second poller in the API process.",
        ))
        futures_mode = values.get("PAPER_MARKET_DATA_MODE", "").strip().lower() == "futures"
        checks.append(PreflightCheck(
            "futures_market_mode", futures_mode,
            "API market mode is Futures." if futures_mode
            else "PAPER_MARKET_DATA_MODE must be futures for the Testnet API profile.",
        ))
        worker_symbols = tuple(
            item.strip().upper() for item in values.get("PAPER_SYMBOLS", "").split(",")
            if item.strip()
        )
        api_symbols_raw = values.get("PAPER_MARKET_SYMBOLS", values.get("PAPER_SYMBOLS", ""))
        api_symbols = tuple(
            item.strip().upper() for item in api_symbols_raw.split(",") if item.strip()
        )
        symbols_match = bool(worker_symbols) and api_symbols == worker_symbols
        checks.append(PreflightCheck(
            "market_symbol_allowlist_match", symbols_match,
            "API and worker market symbol allowlists match."
            if symbols_match
            else "PAPER_MARKET_SYMBOLS must match PAPER_SYMBOLS in the same order; omit it to inherit the worker allowlist.",
        ))
        api_interval = values.get("PAPER_MARKET_INTERVAL", "1m")
        interval_match = api_interval == candle_interval == "1m"
        checks.append(PreflightCheck(
            "market_archive_interval_match", interval_match,
            "API and worker use the shared 1m market archive interval."
            if interval_match
            else "PAPER_MARKET_INTERVAL and PAPER_CANDLE_INTERVAL must both be 1m for the shared archive.",
        ))
        token = values.get("PAPER_API_TOKEN", "")
        token_ok = isinstance(token, str) and len(token) >= 32
        checks.append(PreflightCheck(
            "api_token_strength", token_ok,
            "API bearer token meets the 32-character minimum; token value is not displayed."
            if token_ok else "PAPER_API_TOKEN must contain at least 32 characters.",
        ))
        websocket = _flag_is_true(values, "PAPER_WEBSOCKET_ENABLED")
        checks.append(PreflightCheck(
            "websocket_enabled", websocket,
            "Authenticated read-only WebSocket transport is enabled."
            if websocket else "PAPER_WEBSOCKET_ENABLED must be explicitly true for the Solo staging API.",
        ))
        host = values.get("PAPER_API_HOST", "127.0.0.1").strip().lower()
        loopback = host in _LOCAL_HOSTS
        https_terminated = values.get("PAPER_API_HTTPS_TERMINATED") == "1"
        https_ok = loopback or https_terminated
        checks.append(PreflightCheck(
            "https_termination", https_ok,
            "API bind is loopback or HTTPS termination is explicitly declared."
            if https_ok else "Non-loopback API binds require PAPER_API_HTTPS_TERMINATED=1 at the trusted TLS proxy.",
        ))

    return PreflightReport(role=role, checks=tuple(checks), orders_authorized=False)


def check_database_schema(
    dsn: str,
    *,
    connect_factory: Callable | None = None,
) -> PreflightCheck:
    """Inspect required private tables and RLS flags in a strictly read-only transaction."""
    try:
        from psycopg.conninfo import conninfo_to_dict
        from app.engines.postgres_ssl import postgres_connection_ssl_options
        parameters = conninfo_to_dict(dsn)
        hosts = [item.strip().lower() for item in parameters.get("host", "localhost").split(",") if item.strip()]
        remote_database = any(host not in _LOCAL_HOSTS for host in hosts)
        if connect_factory is None:
            import psycopg
            connect_factory = psycopg.connect
        with connect_factory(dsn, **postgres_connection_ssl_options(dsn)) as connection:
            if remote_database and not getattr(getattr(connection, "info", None), "ssl_in_use", False):
                raise _RemoteDatabaseTLSRequired("TLS is not active for the remote PostgreSQL connection")
            with connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                rows = connection.execute(
                    """
                    SELECT c.relname, c.relrowsecurity
                    FROM pg_class AS c
                    JOIN pg_namespace AS n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'paper_ledger' AND c.relkind = 'r'
                    """
                ).fetchall()
        present = {str(name) for name, _ in rows}
        missing = sorted(REQUIRED_PRIVATE_TABLES - present)
        rls_disabled = sorted(
            str(name) for name, enabled in rows
            if str(name) in REQUIRED_PRIVATE_TABLES and enabled is not True
        )
        if missing or rls_disabled:
            problems = []
            if missing:
                problems.append("missing tables: " + ", ".join(missing))
            if rls_disabled:
                problems.append("row-level security disabled: " + ", ".join(rls_disabled))
            return PreflightCheck("postgres_schema_read_only", False, "; ".join(problems))
        return PreflightCheck(
            "postgres_schema_read_only", True,
            f"Read-only catalog inspection found all {len(REQUIRED_PRIVATE_TABLES)} required private tables with row-level security enabled.",
        )
    except _RemoteDatabaseTLSRequired:
        return PreflightCheck(
            "postgres_schema_read_only", False,
            "TLS is not active for the remote PostgreSQL connection; connection details redacted.",
        )
    except Exception as exc:
        return PreflightCheck(
            "postgres_schema_read_only", False,
            f"Read-only database inspection failed ({type(exc).__name__}); connection details redacted.",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=("worker", "api"))
    parser.add_argument(
        "--check-database", action="store_true",
        help="also inspect the configured PostgreSQL catalog in a READ ONLY transaction",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args(argv)

    report = run_preflight(args.role)
    checks = list(report.checks)
    if args.check_database:
        dsn = os.environ.get("PAPER_DATABASE_URL", "").strip()
        if not dsn:
            checks.append(PreflightCheck(
                "postgres_schema_read_only", False,
                "PAPER_DATABASE_URL is required for the optional read-only schema inspection.",
            ))
        else:
            checks.append(check_database_schema(dsn))
    final_report = PreflightReport(report.role, tuple(checks), orders_authorized=False)

    if args.json:
        print(json.dumps(final_report.to_dict(), sort_keys=True))
    else:
        print(f"Solo staging preflight role={final_report.role} passed={final_report.passed} orders_authorized=False")
        for check in final_report.checks:
            print(f"{'PASS' if check.passed else 'FAIL'} {check.name}: {check.detail}")
    return 0 if final_report.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
