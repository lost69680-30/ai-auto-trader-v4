"""Regression tests for recovering dropped idle PostgreSQL sessions safely."""
from __future__ import annotations

import pytest

from app.engines import postgres_paper_ledger as ledger


class FakeInfo:
    def __init__(self):
        self.transaction_status = ledger.TransactionStatus.IDLE


class FakeCursor:
    description = None
    rowcount = 0

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class FakeRawConnection:
    def __init__(self, *, fail_first_select=False):
        self.closed = False
        self.info = FakeInfo()
        self.queries = []
        self.fail_first_select = fail_first_select

    def execute(self, query, parameters=None):
        self.queries.append(query)
        normalized = query.strip().upper()
        if normalized == "BEGIN":
            self.info.transaction_status = ledger.TransactionStatus.INTRANS
        elif normalized in {"COMMIT", "ROLLBACK"}:
            self.info.transaction_status = ledger.TransactionStatus.IDLE
        if self.fail_first_select and normalized.startswith("SELECT"):
            self.fail_first_select = False
            self.closed = True
            raise ledger.psycopg.OperationalError("server closed the connection")
        return FakeCursor()

    def close(self):
        self.closed = True


def _install_fake_connect(monkeypatch, *, first_connection=None):
    connections = []
    def fake_connect(*args, **kwargs):
        connection = first_connection if not connections and first_connection is not None else FakeRawConnection()
        connections.append(connection)
        return connection
    monkeypatch.setattr(ledger.psycopg, "connect", fake_connect)
    return connections


def test_reconnects_a_closed_idle_session_and_restores_search_path(monkeypatch):
    if ledger.psycopg is None:
        pytest.skip("psycopg is not installed")
    first = FakeRawConnection()
    connections = _install_fake_connect(monkeypatch, first_connection=first)
    connection = ledger._PostgresConnection("postgresql://paper_user:secret@example.invalid:5432/postgres")
    connection.execute("SET search_path TO paper_ledger")

    first.close()
    cursor = connection.execute("SELECT 1")

    assert cursor is not None
    assert len(connections) == 2
    assert "SET search_path TO paper_ledger" in connections[1].queries
    assert "SELECT 1" in connections[1].queries


def test_retries_a_read_once_when_server_drops_idle_session_during_query(monkeypatch):
    if ledger.psycopg is None:
        pytest.skip("psycopg is not installed")
    first = FakeRawConnection(fail_first_select=True)
    connections = _install_fake_connect(monkeypatch, first_connection=first)
    connection = ledger._PostgresConnection("postgresql://paper_user:secret@example.invalid:5432/postgres")

    cursor = connection.execute("SELECT 1")

    assert cursor is not None
    assert len(connections) == 2
    assert "SELECT 1" in connections[1].queries


def test_does_not_replay_side_effecting_select_after_connection_loss(monkeypatch):
    if ledger.psycopg is None:
        pytest.skip("psycopg is not installed")
    first = FakeRawConnection(fail_first_select=True)
    connections = _install_fake_connect(monkeypatch, first_connection=first)
    connection = ledger._PostgresConnection("postgresql://paper_user:secret@example.invalid:5432/postgres")

    with pytest.raises(ledger.psycopg.OperationalError):
        connection.execute("SELECT nextval('paper_sequence')")

    assert len(connections) == 1


def test_does_not_reconnect_after_a_connection_is_lost_inside_a_transaction(monkeypatch):
    if ledger.psycopg is None:
        pytest.skip("psycopg is not installed")
    connections = _install_fake_connect(monkeypatch)
    connection = ledger._PostgresConnection("postgresql://paper_user:secret@example.invalid:5432/postgres")
    connection.execute("BEGIN IMMEDIATE")
    connections[0].close()

    with pytest.raises(RuntimeError, match="transaction"):
        connection.execute("SELECT 1")

    assert len(connections) == 1
    assert connection.in_transaction is False
