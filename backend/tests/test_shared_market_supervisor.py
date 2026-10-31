from backend.shared_market_supervisor import worker_heartbeat_requires_restart


def test_healthy_worker_is_not_restarted():
    assert worker_heartbeat_requires_restart({"status": "HEALTHY", "worker_status": "RUNNING"}, uptime_seconds=300) is False


def test_degraded_but_live_worker_is_not_restarted():
    assert worker_heartbeat_requires_restart({"status": "DEGRADED", "worker_status": "DEGRADED"}, uptime_seconds=300) is False


def test_stale_live_process_is_restarted():
    assert worker_heartbeat_requires_restart({"status": "STALE", "worker_status": "RUNNING", "age_seconds": 120}, uptime_seconds=300) is True


def test_missing_worker_after_startup_grace_is_restarted():
    assert worker_heartbeat_requires_restart({"status": "STARTING", "worker_status": "MISSING"}, uptime_seconds=300) is True


def test_startup_grace_prevents_premature_restart():
    assert worker_heartbeat_requires_restart({"status": "STALE", "worker_status": "RUNNING"}, uptime_seconds=30) is False


def test_unavailable_status_read_is_not_failure_evidence():
    assert worker_heartbeat_requires_restart(None, uptime_seconds=300) is False
