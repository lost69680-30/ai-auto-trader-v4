from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class StagingAndroidBuildContractTests(unittest.TestCase):
    def test_staging_build_uses_a_separate_android_application_id(self):
        gradle = (ROOT / "android/app/build.gradle.kts").read_text(encoding="utf-8")
        self.assertIn('providers.gradleProperty("stagingBuild")', gradle)
        self.assertIn('applicationIdSuffix = ".staging"', gradle)

    def test_staging_workflow_is_locked_to_the_staging_backend(self):
        workflow_path = ROOT / ".github/workflows/build-staging-apk.yml"
        workflow = workflow_path.read_text(encoding="utf-8") if workflow_path.exists() else ""
        self.assertIn("https://solo-futures-websocket-staging.onrender.com", workflow)
        self.assertNotIn("https://ai-auto-trader-v4-backend.onrender.com", workflow)
        self.assertIn("-PstagingBuild=true", workflow)
        self.assertIn("-PpaperApiBaseUrl=", workflow)

    def test_staging_workflow_checks_out_the_triggering_commit_and_base_ancestry(self):
        workflow = (ROOT / ".github/workflows/build-staging-apk.yml").read_text(encoding="utf-8")
        self.assertIn("ref: ${{ github.sha }}", workflow)
        self.assertIn("git merge-base --is-ancestor 1c36bcb8a4e63f33ea5f11f43890258bee6086c7 HEAD", workflow)

    def test_staging_workflow_runs_tests_and_uploads_debug_apk(self):
        workflow = (ROOT / ".github/workflows/build-staging-apk.yml").read_text(encoding="utf-8")
        self.assertIn(":android:app:testDebugUnitTest", workflow)
        self.assertIn(":android:app:assembleDebug", workflow)
        self.assertIn("solo-staging-debug-apk", workflow)
        self.assertIn("dump badging", workflow)
        self.assertIn("com.aiautotrader.v4.staging", workflow)
        self.assertIn("orders_authorized", workflow)


if __name__ == "__main__":
    unittest.main()
