from pathlib import Path


ROOT = Path(__file__).parents[1]
WORKER = ROOT / "android/app/src/main/java/com/aiautotrader/v4/SoloRiskAlertWorker.kt"
PACKAGED_WORKER = ROOT / "extracted-source/ai-auto-trader-v4-assembly/android/app/src/main/java/com/aiautotrader/v4/SoloRiskAlertWorker.kt"


def test_background_worker_retries_persisted_alerts_that_android_did_not_deliver():
    source = WORKER.read_text(encoding="utf-8")
    observe = source.index("SoloNotificationCenter.observeVerifiedRiskSnapshot(applicationContext, risk)")
    retry = source.index("SoloNotificationCenter.getRecords(applicationContext)", observe)
    success = source.index("Result.success()", observe)
    assert observe < retry < success


def test_background_worker_mirror_retries_pending_alert_delivery():
    assert WORKER.read_bytes() == PACKAGED_WORKER.read_bytes()
