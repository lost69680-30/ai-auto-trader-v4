from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "solo-staging-db-restore-drill.yml"

REQUIRED_TABLES = {
    "paper_orders",
    "paper_fills",
    "paper_positions",
    "paper_funding_events",
    "paper_audit_events",
    "paper_system_controls",
    "paper_order_cancellations",
    "paper_position_extrema",
    "paper_equity_snapshots",
    "market_feed_snapshots",
    "market_worker_heartbeats",
    "scheduled_job_definitions",
    "scheduled_jobs",
    "market_candles",
    "market_intelligence_reports",
    "opportunities",
    "opportunity_assessments",
    "opportunity_evidence",
    "opportunity_watch_conditions",
    "opportunity_events",
    "opportunity_claims",
    "strategy_research_experiments",
}


class StagingDatabaseRestoreContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_manual_run_requires_explicit_staging_confirmation(self):
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertIn("confirm_staging_only:", self.workflow)
        self.assertIn("default: false", self.workflow)
        self.assertIn(
            "inputs.confirm_staging_only == true",
            self.workflow,
        )

    def test_source_connection_is_read_only_and_export_is_rls_aware(self):
        self.assertIn("SOLO_BACKUP_DATABASE_URL", self.workflow)
        self.assertIn('PGOPTIONS: "-c default_transaction_read_only=on"', self.workflow)
        self.assertIn("solo_backup_reader", self.workflow)
        self.assertIn("--enable-row-security", self.workflow)
        self.assertIn("--schema=paper_ledger", self.workflow)

    def test_restore_uses_disposable_postgres_and_current_schema_tables(self):
        self.assertIn("restore_test", self.workflow)
        self.assertIn("pg_restore --exit-on-error", self.workflow)
        missing = sorted(table for table in REQUIRED_TABLES if table not in self.workflow)
        self.assertEqual(missing, [], f"restore drill omits current tables: {missing}")
        self.assertNotIn('"$table_count" -ne 14', self.workflow)
        self.assertNotIn("backup_run_id", self.workflow)
        self.assertNotIn("actions/download-artifact", self.workflow)

    def test_restore_checks_ledger_history_market_data_and_stop_control(self):
        for required in (
            "paper_audit_events",
            "paper_system_controls",
            "emergency_stop",
            "market_candles",
            "strategy_research_experiments",
            "market_intelligence_reports",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.workflow)

    def test_restore_drill_never_uploads_plaintext_or_needs_phone_key(self):
        self.assertNotIn("actions/upload-artifact", self.workflow)
        self.assertNotIn("SOLO_BACKUP_AGE_IDENTITY", self.workflow)
        self.assertNotIn("SOLO_BACKUP_AGE_RECIPIENT", self.workflow)
        self.assertNotIn(".dump.age", self.workflow)


if __name__ == "__main__":
    unittest.main()
