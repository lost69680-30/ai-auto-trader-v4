from pathlib import Path
import re
import subprocess


def test_render_worker_health_check_uses_data_aware_readiness_without_stale_deploy_cancellation():
    workflow = (
        Path(__file__).parents[1]
        / ".github/workflows/solo-configure-render-worker-readiness.yml"
    ).read_text(encoding="utf-8")

    assert '"healthCheckPath":"/readyz"' in workflow
    assert '"healthCheckPath":"/healthz"' not in workflow
    assert "/deploys/" not in workflow
    assert "/cancel" not in workflow


def test_render_worker_readiness_workflow_embedded_bash_is_syntax_valid():
    workflow = (
        Path(__file__).parents[1]
        / ".github/workflows/solo-configure-render-worker-readiness.yml"
    ).read_text(encoding="utf-8")
    match = re.search(
        r"(?m)^        run: \|\n((?:^          .*\n|^            .*\n)+)",
        workflow,
    )
    assert match is not None, "expected the Render configuration step to have a bash run block"

    script = "\n".join(line[10:] for line in match.group(1).splitlines())
    completed = subprocess.run(
        ["bash", "-n"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
