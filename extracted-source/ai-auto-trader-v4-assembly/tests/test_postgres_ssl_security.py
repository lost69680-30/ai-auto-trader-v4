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
