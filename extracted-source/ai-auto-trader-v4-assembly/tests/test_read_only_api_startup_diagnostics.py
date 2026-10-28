"""Safety tests for redacted startup diagnostics."""
from app.engines.read_only_api import _safe_startup_error


def test_startup_diagnostic_explains_error_without_leaking_database_uri(monkeypatch):
    dsn = "postgresql://ledger_user:very-secret-password@db.example.test:5432/postgres"
    monkeypatch.setenv("PAPER_DATABASE_URL", dsn)

    diagnostic = _safe_startup_error(RuntimeError(f"connection rejected for {dsn}"))

    assert diagnostic.startswith("RuntimeError:")
    assert "connection rejected for [database URI redacted]" in diagnostic
    assert "very-secret-password" not in diagnostic
    assert dsn not in diagnostic


def test_startup_diagnostic_is_bounded(monkeypatch):
    monkeypatch.delenv("PAPER_DATABASE_URL", raising=False)

    diagnostic = _safe_startup_error(RuntimeError("x" * 500))

    assert len(diagnostic) <= len("RuntimeError: ") + 240
