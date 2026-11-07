from pathlib import Path
import unittest


def repository_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (
            (parent / "android" / "app" / "build.gradle.kts").is_file()
            and (
                parent
                / "extracted-source"
                / "ai-auto-trader-v4-assembly"
                / "android"
                / "app"
                / "build.gradle.kts"
            ).is_file()
            and (parent / ".github" / "workflows" / "build-market-analysis-apk.yml").is_file()
        ):
            return parent
    raise RuntimeError("could not locate Solo repository root")


ROOT = repository_root()
ROOT_GRADLE = ROOT / "android" / "app" / "build.gradle.kts"
EXTRACTED = ROOT / "extracted-source" / "ai-auto-trader-v4-assembly"
EXTRACTED_GRADLE = EXTRACTED / "android" / "app" / "build.gradle.kts"
WORKFLOW = ROOT / ".github" / "workflows" / "build-market-analysis-apk.yml"


class AndroidStagingBuildContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root_gradle = ROOT_GRADLE.read_text(encoding="utf-8")
        cls.extracted_gradle = EXTRACTED_GRADLE.read_text(encoding="utf-8")
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_staging_build_keeps_distinct_installable_package_id(self):
        for content in (self.root_gradle, self.extracted_gradle):
            self.assertIn('applicationId = "com.aiautotrader.v4"', content)
            self.assertIn('applicationIdSuffix = ".staging"', content)
            self.assertIn('resValue("string", "app_name", "Solo Staging")', content)

    def test_staging_workflow_targets_only_the_staging_api(self):
        self.assertIn("https://solo-ai-auto-trader-staging-api.onrender.com", self.workflow)
        self.assertIn("Unexpected staging API target", self.workflow)
        self.assertNotIn('PAPER_API_BASE_URL="https://ai-auto-trader-v4-backend.onrender.com"', self.workflow)
        self.assertNotIn('PAPER_API_BASE_URL="https://solo-futures-websocket-staging.onrender.com"', self.workflow)
        self.assertIn('if grep -Fq \'ai-auto-trader-v4-backend.onrender.com\' "$BUILD_CONFIG"', self.workflow)

    def test_workflow_checks_embedded_url_and_package_before_upload(self):
        self.assertIn("BUILD_CONFIG=", self.workflow)
        self.assertIn("grep -F 'https://solo-ai-auto-trader-staging-api.onrender.com'", self.workflow)
        self.assertIn("package: name='com.aiautotrader.v4.staging'", self.workflow)
        self.assertIn("solo-staging-market-analysis-debug-apk", self.workflow)
        self.assertIn("if-no-files-found: error", self.workflow)

    def test_root_and_render_gradle_configs_are_identical(self):
        self.assertEqual(self.root_gradle, self.extracted_gradle)


if __name__ == "__main__":
    unittest.main()
