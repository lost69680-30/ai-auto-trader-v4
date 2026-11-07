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
    monkeypatch.setenv("PAPER_DATABASE_URL", "postgresql://test")
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


def test_supervisor_restarts_failed_api_without_restarting_worker(monkeypatch):
    import signal

    from backend import shared_market_supervisor as supervisor

    monkeypatch.setenv("PORT", "10000")
    monkeypatch.setenv("PAPER_DATABASE_URL", "postgresql://test")
    clock = {"now": 0.0, "sleeps": 0}
    handlers = []
    started = []

    class FakeProcess:
        def __init__(self, module, generation):
            self.module = module
            self.generation = generation
            self.terminated = False
            self.pid = generation + 200

        def poll(self):
            if self.terminated:
                return 0
            if self.module == "app.engines.read_only_api" and self.generation == 1:
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
        "app.engines.read_only_api",
    ]



def test_supervisor_restarts_live_worker_after_stale_heartbeat(monkeypatch):
    import signal

    from backend import shared_market_supervisor as supervisor

    monkeypatch.setenv("PORT", "10000")
    monkeypatch.setenv("PAPER_DATABASE_URL", "postgresql://test")
    monkeypatch.setenv("PAPER_SUPERVISOR_WORKER_STARTUP_GRACE_SECONDS", "30")
    monkeypatch.setenv("PAPER_SUPERVISOR_WORKER_HEALTH_CHECK_SECONDS", "2")
    clock = {"now": 0.0, "sleeps": 0}
    handlers = []
    started = []

    class FakeStore:
        def __init__(self, *args, **kwargs):
            pass

        def status(self):
            if clock["now"] >= 30:
                return {"status": "STALE", "worker_status": "STALE", "age_seconds": 120, "lease_expired": False}
            return {"status": "HEALTHY", "worker_status": "RUNNING", "age_seconds": 1}

    class FakeProcess:
        def __init__(self, module, generation):
            self.module = module
            self.generation = generation
            self.terminated = False
            self.pid = generation + 300

        def poll(self):
            return 0 if self.terminated else None

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
        clock["now"] += 5.0
        if len(started) >= 3:
            handlers[-1](signal.SIGTERM, None)

    from backend import shared_market_store
    monkeypatch.setattr(shared_market_store, "PostgresMarketFeedStore", FakeStore)
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
    assert started[0][2].terminated is True
    assert started[1][2].terminated is True


def test_supervisor_shutdown_grace_exceeds_worker_scheduler_grace():
    from backend.shared_market_supervisor import configured_child_shutdown_grace

    assert configured_child_shutdown_grace({}) == 25.0
    assert configured_child_shutdown_grace({"PAPER_SUPERVISOR_CHILD_SHUTDOWN_GRACE_SECONDS": "40"}) == 40.0
    assert configured_child_shutdown_grace({"PAPER_SUPERVISOR_CHILD_SHUTDOWN_GRACE_SECONDS": "5"}) == 15.0


def test_supervisor_mode_defaults_to_both_processes():
    from backend.shared_market_supervisor import configured_supervisor_modules

    modules = configured_supervisor_modules("all", {}, {})
    assert set(modules) == {"worker", "api"}


def test_worker_only_supervisor_does_not_start_a_second_api():
    from backend.shared_market_supervisor import configured_supervisor_modules

    modules = configured_supervisor_modules("worker", {"role": "worker"}, {"role": "api"})
    assert modules == {"worker": ("backend.shared_market_worker", {"role": "worker"})}


def test_api_only_supervisor_does_not_start_a_second_worker():
    from backend.shared_market_supervisor import configured_supervisor_modules

    modules = configured_supervisor_modules("api", {"role": "worker"}, {"role": "api"})
    assert modules == {"api": ("app.engines.read_only_api", {"role": "api"})}


def test_supervisor_rejects_unknown_mode():
    from backend.shared_market_supervisor import configured_supervisor_modules

    with pytest.raises(ValueError, match="PAPER_MARKET_SUPERVISOR_MODE"):
        configured_supervisor_modules("both-ish", {}, {})


def test_worker_only_health_server_exposes_read_only_healthz():
    import json
    from urllib.error import HTTPError
    from urllib.request import urlopen

    from backend.shared_market_supervisor import start_worker_health_server

    server = start_worker_health_server(0, host="127.0.0.1")
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/healthz", timeout=2) as response:
            body = json.loads(response.read())
            assert response.status == 200
            assert body == {
                "status": "ok",
                "mode": "PAPER_ONLY",
                "orders_authorized": False,
            }
        try:
            urlopen(f"http://127.0.0.1:{server.server_port}/readyz", timeout=2)
        except HTTPError as error:
            assert error.code == 503
            readiness = json.loads(error.read())
            assert readiness["status"] == "not_ready"
            assert readiness["mode"] == "PAPER_ONLY"
            assert readiness["worker_status"] == "UNKNOWN"
            assert readiness["orders_authorized"] is False
        else:
            raise AssertionError("readiness without a provider unexpectedly returned 2xx")
    finally:
        server.shutdown()
        server.server_close()

def test_worker_heartbeat_requires_restart_for_stale_lease_even_when_process_is_running():
    from backend.shared_market_supervisor import worker_heartbeat_requires_restart

    assert worker_heartbeat_requires_restart(
        {"status": "STALE", "worker_status": "STALE", "age_seconds": 120, "lease_expired": False},
        uptime_seconds=300,
        startup_grace_seconds=30,
    ) is True

def test_worker_readyz_returns_200_for_fresh_healthy_worker_and_paper_only_state():
    import json
    from datetime import datetime, timezone
    from urllib.request import urlopen

    from backend.shared_market_supervisor import start_worker_health_server

    state = {
        "status": "HEALTHY",
        "worker_status": "RUNNING",
        "age_seconds": 4.0,
        "lease_expired": False,
        "orders_authorized": False,
        "process_alive": True,
        "details": {
            "scheduled_jobs": {
                "status": "RUNNING",
                "last_poll_at": datetime.now(timezone.utc).isoformat(),
            },
        },
    }
    server = start_worker_health_server(
        0, host="127.0.0.1",
        readiness_provider=lambda: dict(state),
        max_age_seconds=90,
        max_scheduler_stale_seconds=900,
    )
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/readyz", timeout=2) as response:
            body = json.loads(response.read())
        assert response.status == 200
        assert body["status"] == "ready"
        assert body["mode"] == "PAPER_ONLY"
        assert body["worker_status"] == "RUNNING"
        assert body["process_alive"] is True
        assert body["scheduler_status"] == "RUNNING"
        assert body["scheduler_age_seconds"] <= 900
        assert body["orders_authorized"] is False
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    "override",
    [
        {"status": "STALE"},
        {"worker_status": "STALE"},
        {"age_seconds": 120.0},
        {"age_seconds": -1.0},
        {"age_seconds": True},
        {"lease_expired": True},
        {"process_alive": False},
        {"orders_authorized": True},
        {"details": {"scheduled_jobs": {"status": "DEGRADED", "last_poll_at": "2026-11-07T01:00:00+00:00"}}},
        {"details": {"scheduled_jobs": {"status": "RUNNING", "last_poll_at": "2026-10-01T00:00:00+00:00"}}},
    ],
)
def test_worker_readyz_returns_503_for_unhealthy_or_unsafe_state(override):
    import json
    from datetime import datetime, timezone
    from urllib.error import HTTPError
    from urllib.request import urlopen

    from backend.shared_market_supervisor import start_worker_health_server

    state = {
        "status": "HEALTHY",
        "worker_status": "RUNNING",
        "age_seconds": 4.0,
        "lease_expired": False,
        "orders_authorized": False,
        "process_alive": True,
        "details": {
            "scheduled_jobs": {
                "status": "RUNNING",
                "last_poll_at": datetime.now(timezone.utc).isoformat(),
            },
        },
    }
    state.update(override)
    server = start_worker_health_server(
        0, host="127.0.0.1",
        readiness_provider=lambda: dict(state),
        max_age_seconds=90,
        max_scheduler_stale_seconds=900,
    )
    try:
        try:
            urlopen(f"http://127.0.0.1:{server.server_port}/readyz", timeout=2)
        except HTTPError as error:
            assert error.code == 503
            body = json.loads(error.read())
            assert body["status"] == "not_ready"
            assert body["mode"] == "PAPER_ONLY"
            expected_authorization = override.get("orders_authorized", False)
            assert body["orders_authorized"] is expected_authorization
            expected_mode = "SAFETY_VIOLATION" if expected_authorization is True else "PAPER_ONLY"
            assert body["mode"] == expected_mode
        else:
            raise AssertionError("unhealthy worker readiness unexpectedly returned 2xx")
    finally:
        server.shutdown()
        server.server_close()


def test_worker_readyz_fails_closed_when_readiness_provider_raises():
    import json
    from urllib.error import HTTPError
    from urllib.request import urlopen

    from backend.shared_market_supervisor import start_worker_health_server

    def unavailable():
        raise RuntimeError("database credentials and DSN must not be exposed")

    server = start_worker_health_server(
        0, host="127.0.0.1",
        readiness_provider=unavailable,
        max_age_seconds=90,
    )
    try:
        try:
            urlopen(f"http://127.0.0.1:{server.server_port}/readyz", timeout=2)
        except HTTPError as error:
            assert error.code == 503
            body = json.loads(error.read())
            assert body == {
                "status": "not_ready",
                "mode": "PAPER_ONLY",
                "worker_status": "UNKNOWN",
                "feed_status": "UNKNOWN",
                "scheduler_status": "UNKNOWN",
                "scheduler_age_seconds": None,
                "process_alive": False,
                "lease_expired": None,
                "age_seconds": None,
                "orders_authorized": False,
            }
        else:
            raise AssertionError("readiness provider failure unexpectedly returned 2xx")
    finally:
        server.shutdown()
        server.server_close()
