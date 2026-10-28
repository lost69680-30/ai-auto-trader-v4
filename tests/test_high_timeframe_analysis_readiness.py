from pathlib import Path

ROOT = Path(__file__).parents[1]
ANDROID_SOURCE = ROOT / "android/app/src/main/java/com/aiautotrader/v4/MainActivity.kt"
ROOT_API = ROOT / "app/engines/read_only_api.py"
PACKAGE_API = ROOT / "extracted-source/ai-auto-trader-v4-assembly/app/engines/read_only_api.py"


def test_default_candle_limit_supports_longer_calendar_history():
    root_api = ROOT_API.read_text(encoding="utf-8")
    package_api = PACKAGE_API.read_text(encoding="utf-8")
    assert root_api == package_api
    assert 'PAPER_ANALYSIS_CANDLE_LIMIT", "500"' in root_api
    assert "analysis_candle_limit = int(" in root_api


def test_high_timeframe_directional_readiness_does_not_require_ema50():
    source = ANDROID_SOURCE.read_text(encoding="utf-8")
    assert "internal fun classifyTimeframeBias(" in source
    assert "val directionalReady: Boolean" in source
    assert "analysis.directionalReady" in source
    assert "EMA50 unavailable" in source
    assert "core trend/momentum" in source
    assert "NOT CALIBRATED" in source


def test_short_or_conflicting_history_never_forces_directional_bias():
    source = ANDROID_SOURCE.read_text(encoding="utf-8")
    assert 'if (!directionalReady) return "INSUFFICIENT DATA"' in source
    assert 'else -> "NEUTRAL / MIXED"' in source
    assert 'CONFLICT · bullish and bearish timeframes disagree' in source
    assert 'orders_authorized' in source

def test_postgres_paper_ledger_sources_and_driver_are_mirrored():
    root_engines = ROOT / "app/engines"
    package_engines = ROOT / "extracted-source/ai-auto-trader-v4-assembly/app/engines"
    for name in (
        "postgres_paper_ledger.py",
        "postgres_ssl.py",
        "sqlite_to_postgres_import.py",
    ):
        assert (root_engines / name).read_text(encoding="utf-8") == (package_engines / name).read_text(encoding="utf-8")
    root_project = ROOT / "pyproject.toml"
    package_project = ROOT / "extracted-source/ai-auto-trader-v4-assembly/pyproject.toml"

    dependency = 'dependencies = ["psycopg[binary]==3.3.6", "asgiref>=3.8,<4", "uvicorn[standard]>=0.30,<1"]'
    assert dependency in root_project.read_text(encoding="utf-8")
    assert dependency in package_project.read_text(encoding="utf-8")

def test_remote_postgres_ledger_connections_require_encrypted_transport():
    from app.engines.postgres_ssl import postgres_connection_ssl_options

    assert postgres_connection_ssl_options(
        "postgresql://ledger_user:example-password@db.example.com:5432/postgres"
    ) == {"sslmode": "require"}
    assert postgres_connection_ssl_options(
        "postgresql://ledger_user:example-password@db.example.com:5432/postgres?sslmode=verify-full"
    ) == {}
