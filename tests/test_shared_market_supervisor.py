import pytest

from backend.shared_market_supervisor import build_child_environments


def test_supervisor_separates_api_and_worker_environment_without_leaking_api_token():
    parent = {
        "PORT": "10000",
        "PAPER_DATABASE_URL": "postgresql://staging-db",
        "PAPER_API_TOKEN": "secret-api-token",
        "PAPER_MARKET_TESTNET": "true",
        "PAPER_MARKET_WORKER_ID": "solo-market-worker",
        "BINANCE_SPOT_TESTNET_API_KEY": "exchange-key",
        "BINANCE_SPOT_TESTNET_API_SECRET": "exchange-secret",
    }
    worker_env, api_env = build_child_environments(parent)

    assert worker_env["PAPER_MARKET_ROLE"] == "worker"
    assert worker_env["PAPER_MARKET_SUPERVISOR_ENABLED"] == "false"
    assert worker_env["PAPER_DATABASE_URL"] == parent["PAPER_DATABASE_URL"]
    assert worker_env["PAPER_MARKET_TESTNET"] == "true"
    assert "PAPER_API_TOKEN" not in worker_env
    assert "BINANCE_SPOT_TESTNET_API_KEY" not in worker_env
    assert "BINANCE_SPOT_TESTNET_API_SECRET" not in worker_env
    assert api_env["PAPER_MARKET_ROLE"] == "api"
    assert api_env["PAPER_MARKET_SUPERVISOR_ENABLED"] == "false"
    assert api_env["PAPER_RESEARCH_WORKER_ENABLED"] == "false"
    assert api_env["PAPER_API_HOST"] == "0.0.0.0"
    assert api_env["PAPER_API_PORT"] == "10000"
    assert api_env["PAPER_API_HTTPS_TERMINATED"] == "1"
    assert api_env["PAPER_API_TOKEN"] == "secret-api-token"
    assert "BINANCE_SPOT_TESTNET_API_KEY" not in api_env
    assert "BINANCE_SPOT_TESTNET_API_SECRET" not in api_env
    assert parent["PAPER_API_TOKEN"] == "secret-api-token"


def test_supervisor_requires_render_port():
    with pytest.raises(ValueError, match="PORT"):
        build_child_environments({"PAPER_DATABASE_URL": "postgresql://staging-db"})
