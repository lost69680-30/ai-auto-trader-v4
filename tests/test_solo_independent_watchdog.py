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


if __name__ == "__main__":
    unittest.main()
