"""Linux process-level recovery tests for Solo's real supervisor subprocesses.

These tests use disposable fake child modules. They exercise the actual supervisor
and operating-system process lifecycle without connecting to PostgreSQL, exchanges,
Render, Supabase, or any trading endpoint.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR = ROOT / "backend" / "shared_market_supervisor.py"


def _write_fake_modules(root: Path) -> None:
    (root / "backend").mkdir()
    (root / "app" / "engines").mkdir(parents=True)
    (root / "backend" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "engines" / "__init__.py").write_text("", encoding="utf-8")

    child = '''
import os
from pathlib import Path
import time

name = os.environ["TEST_CHILD_NAME"]
events = Path(os.environ["TEST_EVENT_FILE"])
fail_name = os.environ.get("TEST_FAIL_CHILD", "")
lines = events.read_text(encoding="utf-8").splitlines() if events.exists() else []
count = sum(line.split()[0] == name for line in lines) + 1
with events.open("a", encoding="utf-8") as stream:
    stream.write(f"{name} {os.getpid()} {count}\\n")
if name == fail_name and count == 1:
    raise SystemExit(23)
while True:
    time.sleep(0.1)
'''
    (root / "backend" / "shared_market_worker.py").write_text(child, encoding="utf-8")
    (root / "app" / "engines" / "read_only_api.py").write_text(child, encoding="utf-8")


def _read_events(path: Path) -> list[tuple[str, int, int]]:
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        name, pid, count = line.split()
        result.append((name, int(pid), int(count)))
    return result


def _wait_for(predicate, process: subprocess.Popen, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        if process.poll() is not None:
            raise AssertionError(f"supervisor exited early with code {process.returncode}")
        time.sleep(0.1)
    raise AssertionError("timed out waiting for real child-process recovery")


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _exercise_restart(failed_child: str) -> None:
    with tempfile.TemporaryDirectory(prefix="solo-supervisor-process-test-") as directory:
        temp = Path(directory)
        modules = temp / "modules"
        modules.mkdir()
        _write_fake_modules(modules)
        events = temp / "events.log"
        supervisor_script = temp / "shared_market_supervisor.py"
        supervisor_script.write_text(SUPERVISOR.read_text(encoding="utf-8"), encoding="utf-8")

        env = os.environ.copy()
        env.update({
            "PYTHONPATH": str(modules),
            "PORT": "18080",
            "TEST_EVENT_FILE": str(events),
            "TEST_FAIL_CHILD": failed_child,
            "PYTHONUNBUFFERED": "1",
        })
        supervisor = subprocess.Popen(
            [sys.executable, str(supervisor_script)],
            cwd=temp,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            def recovered() -> bool:
                rows = _read_events(events)
                failed_name = "worker" if failed_child == "worker" else "api"
                stable_name = "api" if failed_child == "worker" else "worker"
                failed_rows = [row for row in rows if row[0] == failed_name]
                stable_rows = [row for row in rows if row[0] == stable_name]
                return len(failed_rows) >= 2 and len(stable_rows) == 1

            _wait_for(recovered, supervisor)
            rows = _read_events(events)
            failed_name = "worker" if failed_child == "worker" else "api"
            stable_name = "api" if failed_child == "worker" else "worker"
            assert len([row for row in rows if row[0] == failed_name]) >= 2
            assert len([row for row in rows if row[0] == stable_name]) == 1

            supervisor.send_signal(signal.SIGTERM)
            supervisor.wait(timeout=12)
            assert supervisor.returncode == 0
            # Every child recorded by the harness, including the restarted child,
            # must be gone after graceful supervisor shutdown.
            for _, pid, _ in rows:
                deadline = time.monotonic() + 3
                while _pid_is_alive(pid) and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert not _pid_is_alive(pid), f"child process {pid} survived supervisor shutdown"
        finally:
            if supervisor.poll() is None:
                supervisor.kill()
                supervisor.wait(timeout=5)
            if supervisor.stdout:
                supervisor.stdout.close()


def test_real_worker_process_restarts_without_restarting_api_and_shuts_down():
    _exercise_restart("worker")


def test_real_api_process_restarts_without_restarting_worker_and_shuts_down():
    _exercise_restart("api")
