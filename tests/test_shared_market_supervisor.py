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


def test_supervisor_restarts_failed_worker_without_restarting_api(monkeypatch):
    import signal

    from backend import shared_market_supervisor as supervisor

    monkeypatch.setenv("PORT", "10000")
    clock = {"now": 0.0, "sleeps": 0}
    handlers = []
    started = []

    class FakeProcess:
        def __init__(self, module, generation):
            self.module = module
            self.generation = generation
            self.terminated = False
            self.pid = generation + 100

        def poll(self):
            if self.terminated:
                return 0
            if self.module == "backend.shared_market_worker" and self.generation == 0:
                return 1
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    def fake_start(module, env):
        process = FakeProcess(module, len(started))
        started.append((module, env, process))
        return process

    def fake_sleep(seconds):
        clock["sleeps"] += 1
        clock["now"] += 2.0
        if clock["sleeps"] == 2:
            handlers[-1](signal.SIGTERM, None)

    monkeypatch.setattr(supervisor, "_start", fake_start)
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(supervisor.time, "sleep", fake_sleep)
    monkeypatch.setattr(supervisor.signal, "signal", lambda sig, handler: handlers.append(handler))

    supervisor.main()

    assert [item[0] for item in started] == [
        "backend.shared_market_worker",
        "app.engines.read_only_api",
        "backend.shared_market_worker",
    ]
