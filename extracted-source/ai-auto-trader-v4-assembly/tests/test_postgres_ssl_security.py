from app.engines.postgres_ssl import postgres_connection_ssl_options


def test_remote_postgres_connection_requires_tls_by_default():
    assert postgres_connection_ssl_options(
        "postgresql://ledger_user:example-password@db.example.com:5432/postgres"
    ) == {"sslmode": "require"}


def test_remote_postgres_connection_does_not_downgrade_verified_tls():
    assert postgres_connection_ssl_options(
        "postgresql://ledger_user:example-password@db.example.com:5432/postgres?sslmode=verify-full"
    ) == {}


def test_remote_postgres_connection_upgrades_insecure_sslmode():
    assert postgres_connection_ssl_options(
        "postgresql://ledger_user:example-password@db.example.com:5432/postgres?sslmode=disable"
    ) == {"sslmode": "require"}


def test_local_disposable_database_can_run_without_tls():
    assert postgres_connection_ssl_options(
        "postgresql://postgres:postgres@127.0.0.1:5432/postgres?sslmode=disable"
    ) == {}

def test_empty_database_url_fails_closed_without_creating_sqlite_file(monkeypatch, tmp_path):
    import pytest

    from app.engines.read_only_api import _open_ledger

    database_path = tmp_path / "must-not-fallback.sqlite3"
    monkeypatch.setenv("PAPER_DATABASE_URL", "")
    with pytest.raises(RuntimeError, match="refusing local SQLite fallback"):
        _open_ledger(str(database_path))
    assert not database_path.exists()
