package com.aiautotrader.v4

import android.content.Context
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.util.concurrent.TimeUnit

internal const val SOLO_BACKGROUND_RISK_POLL_INTERVAL_MINUTES = 15L
private const val SOLO_BACKGROUND_RISK_WORK_NAME = "solo_verified_background_risk_alerts"

internal fun isEligibleForBackgroundRiskPolling(baseUrl: String, token: String): Boolean =
    baseUrl.trim().startsWith("https://") &&
        baseUrl.trim().length > "https://".length &&
        token.length >= 32 &&
        token.any { !it.isWhitespace() }

class SoloRiskAlertWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {
    override suspend fun doWork(): Result {
        val token = BackendTokenStore(applicationContext).read().orEmpty()
        val baseUrl = BuildConfig.PAPER_API_BASE_URL
        if (!isEligibleForBackgroundRiskPolling(baseUrl, token)) return Result.success()

        return try {
            val risk = withContext(Dispatchers.IO) {
                fetchPaperRiskSnapshot(baseUrl, token)
            }
            SoloNotificationCenter.observeVerifiedRiskSnapshot(applicationContext, risk)
            Result.success()
        } catch (_: Exception) {
            // A timeout, sleeping free-tier host, auth transition, or invalid response
            // never creates an alert. WorkManager retries with bounded backoff.
            Result.retry()
        }
    }
}

internal object SoloRiskAlertWork {
    fun enqueue(context: Context) {
        val constraints = Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED)
            .build()
        val request = PeriodicWorkRequestBuilder<SoloRiskAlertWorker>(
            SOLO_BACKGROUND_RISK_POLL_INTERVAL_MINUTES,
            TimeUnit.MINUTES,
        )
            .setConstraints(constraints)
            .setBackoffCriteria(
                androidx.work.BackoffPolicy.EXPONENTIAL,
                30,
                TimeUnit.SECONDS,
            )
            .build()
        WorkManager.getInstance(context.applicationContext).enqueueUniquePeriodicWork(
            SOLO_BACKGROUND_RISK_WORK_NAME,
            ExistingPeriodicWorkPolicy.KEEP,
            request,
        )
    }
}
