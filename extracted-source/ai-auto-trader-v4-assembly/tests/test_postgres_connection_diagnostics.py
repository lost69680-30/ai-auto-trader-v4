"""Tests for safe PostgreSQL connection diagnostics."""
import pytest

from app.engines import postgres_paper_ledger as ledger


def test_postgres_connection_error_includes_safe_driver_diagnostic(monkeypatch):
    if ledger.psycopg is None:
        pytest.skip("psycopg is not installed")

    def fail_connect(*args, **kwargs):
        raise ledger.psycopg.ProgrammingError("invalid connection option 'sslmod'")

    monkeypatch.setattr(ledger.psycopg, "connect", fail_connect)
    with pytest.raises(RuntimeError) as caught:
        ledger._PostgresConnection("postgresql://paper_user:secret-value@example.invalid:5432/postgres")

    message = str(caught.value)
    assert "ProgrammingError" in message
    assert "invalid connection option" in message
    assert "secret-value" not in message
    assert "paper_user:secret-value" not in message


def test_postgres_connection_error_redacts_password_from_driver_text(monkeypatch):
    if ledger.psycopg is None:
        pytest.skip("psycopg is not installed")

    def fail_connect(*args, **kwargs):
        raise ledger.psycopg.ProgrammingError("bad password=top-secret-value option")

    monkeypatch.setattr(ledger.psycopg, "connect", fail_connect)
    with pytest.raises(RuntimeError) as caught:
        ledger._PostgresConnection("postgresql://paper_user:secret-value@example.invalid:5432/postgres")

    message = str(caught.value)
    assert "top-secret-value" not in message
    assert "password=[redacted]" in message
