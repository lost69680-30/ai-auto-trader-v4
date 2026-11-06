import unittest

from scripts.solo_independent_watchdog import classify_readiness, valid_worker_liveness


class IndependentWatchdogClassificationTests(unittest.TestCase):
    def test_ready_running_paper_only_worker_is_healthy(self):
        self.assertEqual(
            classify_readiness({
                "status": "ready",
                "worker_status": "RUNNING",
                "orders_authorized": False,
            })[0],
            "healthy",
        )

    def test_stale_worker_heartbeat_is_confirmed_failure(self):
        self.assertEqual(
            classify_readiness({
                "status": "not_ready",
                "worker_status": "STALE",
                "orders_authorized": False,
            })[0],
            "worker_failed",
        )

    def test_stopped_error_and_missing_heartbeats_are_confirmed_failures(self):
        for status in ("STOPPED", "ERROR", "MISSING"):
            with self.subTest(status=status):
                self.assertEqual(
                    classify_readiness({
                        "status": "not_ready",
                        "worker_status": status,
                        "orders_authorized": False,
                    })[0],
                    "worker_failed",
                )

    def test_stale_market_feed_with_running_worker_is_not_restart_authority(self):
        state, _, _ = classify_readiness({
            "status": "not_ready",
            "worker_status": "RUNNING",
            "orders_authorized": False,
        })
        self.assertEqual(state, "indeterminate")

    def test_api_error_unknown_state_and_invalid_payload_fail_closed(self):
        self.assertEqual(classify_readiness(None, "TimeoutError")[0], "indeterminate")
        self.assertEqual(classify_readiness({
            "status": "not_ready", "worker_status": "UNKNOWN",
            "orders_authorized": False,
        })[0], "indeterminate")
        self.assertEqual(classify_readiness({
            "status": "not_ready", "worker_status": "STALE",
            "orders_authorized": True,
        })[0], "indeterminate")

    def test_worker_liveness_requires_explicit_paper_only_safety_fields(self):
        self.assertTrue(valid_worker_liveness({
            "status": "ok", "mode": "PAPER_ONLY", "orders_authorized": False,
        }))
        self.assertFalse(valid_worker_liveness({
            "status": "ok", "mode": "LIVE", "orders_authorized": False,
        }))
        self.assertFalse(valid_worker_liveness({
            "status": "ok", "mode": "PAPER_ONLY", "orders_authorized": True,
        }))
        self.assertFalse(valid_worker_liveness(None, "TimeoutError"))


    def test_healthy_worker_never_requests_render_restart(self):
        from unittest.mock import patch
        from scripts import solo_independent_watchdog as watchdog

        with patch.dict("os.environ", {
            "API_READY_URL": "https://api.example/readyz",
            "WORKER_LIVENESS_URL": "https://worker.example/healthz",
            "RENDER_WORKER_SERVICE_ID": "srv-paper-only",
            "RENDER_API_KEY": "test-key",
        }, clear=True), patch.object(watchdog, "get_json", side_effect=[
            ({"status": "ok", "mode": "PAPER_ONLY", "orders_authorized": False}, None),
            ({"status": "ready", "worker_status": "RUNNING", "orders_authorized": False}, None),
        ]), patch.object(watchdog.urllib.request, "urlopen") as restart, patch.object(watchdog.time, "sleep") as sleep:
            self.assertEqual(watchdog.main(), 0)
            restart.assert_not_called()
            sleep.assert_not_called()

    def test_indeterminate_readiness_never_requests_render_restart(self):
        from unittest.mock import patch
        from scripts import solo_independent_watchdog as watchdog

        with patch.dict("os.environ", {
            "API_READY_URL": "https://api.example/readyz",
            "WORKER_LIVENESS_URL": "https://worker.example/healthz",
            "RENDER_WORKER_SERVICE_ID": "srv-paper-only",
            "RENDER_API_KEY": "test-key",
        }, clear=True), patch.object(watchdog, "get_json", side_effect=[
            ({"status": "ok", "mode": "PAPER_ONLY", "orders_authorized": False}, None),
            ({"status": "not_ready", "worker_status": "RUNNING", "orders_authorized": False}, None),
        ]), patch.object(watchdog.urllib.request, "urlopen") as restart, patch.object(watchdog.time, "sleep") as sleep:
            self.assertEqual(watchdog.main(), 1)
            restart.assert_not_called()
            sleep.assert_not_called()

    def test_confirmed_stale_worker_requests_exactly_one_restart_and_verifies_recovery(self):
        from unittest.mock import patch
        from scripts import solo_independent_watchdog as watchdog

        class AcceptedRestart:
            status = 202
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        healthy_liveness = {"status": "ok", "mode": "PAPER_ONLY", "orders_authorized": False}
        stale = {"status": "not_ready", "worker_status": "STALE", "orders_authorized": False}
        ready = {"status": "ready", "worker_status": "RUNNING", "orders_authorized": False}
        with patch.dict("os.environ", {
            "API_READY_URL": "https://api.example/readyz",
            "WORKER_LIVENESS_URL": "https://worker.example/healthz",
            "RENDER_WORKER_SERVICE_ID": "srv-paper-only",
            "RENDER_API_KEY": "test-key",
        }, clear=True), patch.object(watchdog, "get_json", side_effect=[
            (healthy_liveness, None), (stale, None), (stale, None),
            (healthy_liveness, None), (stale, None), (ready, None),
        ]), patch.object(watchdog.urllib.request, "urlopen", return_value=AcceptedRestart()) as restart, patch.object(watchdog.time, "sleep") as sleep:
            self.assertEqual(watchdog.main(), 0)
            restart.assert_called_once()
            request = restart.call_args.args[0]
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.full_url, "https://api.render.com/v1/services/srv-paper-only/restart")
            sleep.assert_any_call(60)
            sleep.assert_any_call(20)

    def test_missing_render_secret_reports_failure_without_attempting_restart(self):
        from unittest.mock import patch
        from scripts import solo_independent_watchdog as watchdog

        stale = {"status": "not_ready", "worker_status": "STALE", "orders_authorized": False}
        with patch.dict("os.environ", {
            "API_READY_URL": "https://api.example/readyz",
            "WORKER_LIVENESS_URL": "https://worker.example/healthz",
            "RENDER_WORKER_SERVICE_ID": "srv-paper-only",
        }, clear=True), patch.object(watchdog, "get_json", side_effect=[
            (None, "TimeoutError"), (stale, None), (stale, None),
        ]), patch.object(watchdog.urllib.request, "urlopen") as restart, patch.object(watchdog.time, "sleep"):
            self.assertEqual(watchdog.main(), 1)
            restart.assert_not_called()


if __name__ == "__main__":
    unittest.main()
