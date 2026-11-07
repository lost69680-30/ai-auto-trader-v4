from pathlib import Path


def test_render_worker_health_check_uses_data_aware_readiness_without_stale_deploy_cancellation():
    workflow = (
        Path(__file__).parents[1]
        / ".github/workflows/solo-configure-render-worker-readiness.yml"
    ).read_text(encoding="utf-8")

    assert '"healthCheckPath":"/readyz"' in workflow
    assert '"healthCheckPath":"/healthz"' not in workflow
    assert "/deploys/" not in workflow
    assert "/cancel" not in workflow
