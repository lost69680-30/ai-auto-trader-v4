package com.aiautotrader.v4

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import org.json.JSONArray
import org.json.JSONObject

internal data class SoloNotificationRecord(
    val eventId: String,
    val type: String,
    val severity: String,
    val title: String,
    val message: String,
    val occurredAtEpochMs: Long,
    val acknowledged: Boolean,
    val resolved: Boolean,
    val osDelivered: Boolean,
)

internal enum class SoloAlertTransitionAction { NONE, PUBLISH, RESOLVE }

internal fun evaluateSoloAlertTransition(
    previousActive: Boolean?,
    currentActive: Boolean,
): SoloAlertTransitionAction = when {
    previousActive == null && currentActive -> SoloAlertTransitionAction.PUBLISH
    previousActive == null -> SoloAlertTransitionAction.NONE
    !previousActive && currentActive -> SoloAlertTransitionAction.PUBLISH
    previousActive && !currentActive -> SoloAlertTransitionAction.RESOLVE
    else -> SoloAlertTransitionAction.NONE
}

internal object SoloNotificationCenter {
    private val admissionGate = SoloNotificationGate()
    private const val PREFS = "solo_notification_center"
    private const val EVENTS = "events"
    private const val LAST_STOP_STATE = "last_emergency_stop_state"
    private const val LAST_MARKS_STATE = "last_marks_fresh_state"
    private const val CRITICAL_CHANNEL_ID = "solo_critical_alerts"
    private const val HIGH_CHANNEL_ID = "solo_high_alerts"
    private const val MAX_EVENTS = 100

    fun observeVerifiedRiskSnapshot(context: Context, risk: PaperRiskView) {
        observeBooleanTransition(
            context = context,
            preferenceKey = LAST_STOP_STATE,
            current = risk.emergencyStopActive,
            type = "EMERGENCY_STOP",
            severity = "CRITICAL",
            title = "Emergency Stop active",
            message = "Emergency Stop is confirmed active. New paper orders are blocked.",
        )
        risk.marksFresh?.let { fresh ->
            observeBooleanTransition(
                context = context,
                preferenceKey = LAST_MARKS_STATE,
                current = !fresh,
                type = "MARKET_DATA_STALE",
                severity = "HIGH",
                title = "Paper position marks are stale",
                message = "The verified paper-risk snapshot reports stale position marks. Treat current position values as uncertain.",
            )
        }
    }

    private fun observeBooleanTransition(
        context: Context,
        preferenceKey: String,
        current: Boolean,
        type: String,
        severity: String,
        title: String,
        message: String,
    ) {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val previousActive = when (prefs.getString(preferenceKey, null)) {
            "ACTIVE" -> true
            "INACTIVE" -> false
            else -> null
        }
        val action = evaluateSoloAlertTransition(previousActive, current)
        if (action == SoloAlertTransitionAction.NONE) return
        prefs.edit().putString(preferenceKey, if (current) "ACTIVE" else "INACTIVE").apply()
        when (action) {
            SoloAlertTransitionAction.PUBLISH -> publish(context, type, severity, title, message)
            SoloAlertTransitionAction.RESOLVE -> resolveLatest(context, type)
            SoloAlertTransitionAction.NONE -> Unit
        }
    }

    private fun publish(
        context: Context,
        type: String,
        severity: String,
        title: String,
        message: String,
    ) {
        val alertType = SoloAlertType.values().firstOrNull { it.name == type } ?: return
        if (alertType.requiredSeverity.name != severity) return
        val now = System.currentTimeMillis()
        val eventId = "$type:$now"
        val admission = evaluateSoloAlertAdmission(
            gate = admissionGate,
            eventId = eventId,
            type = alertType,
            conditionKey = "active",
            title = title,
            message = message,
            occurredAtEpochMs = now,
            nowEpochMs = now,
            source = "paper_risk_snapshot",
            scope = if (alertType == SoloAlertType.EMERGENCY_STOP) "account" else "positions",
        )
        if (admission != SoloNotificationDecision.ACCEPTED) return
        val records = readRecords(context).toMutableList()
        records.add(
            0,
            SoloNotificationRecord(
                eventId, type, severity, title, message, now,
                acknowledged = false, resolved = false, osDelivered = false,
            )
        )
        // Persist the in-app alert before attempting OS delivery so permission or
        // notification-manager failures cannot erase the alert from Solo history.
        saveRecords(context, records.take(MAX_EVENTS))
        if (postSystemNotification(context, eventId, title, message, severity)) {
            updateRecord(context, eventId) { it.copy(osDelivered = true) }
        }
    }

    private fun postSystemNotification(
        context: Context,
        eventId: String,
        title: String,
        message: String,
        severity: String,
    ): Boolean {
        return try {
            val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            val channelId = if (severity == "CRITICAL") CRITICAL_CHANNEL_ID else HIGH_CHANNEL_ID
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                manager.createNotificationChannel(
                    NotificationChannel(
                        channelId,
                        if (severity == "CRITICAL") "Solo critical alerts" else "Solo high-priority alerts",
                        if (severity == "CRITICAL") NotificationManager.IMPORTANCE_HIGH
                        else NotificationManager.IMPORTANCE_DEFAULT,
                    ).apply {
                        description = "Verified Solo risk and market-data alerts"
                        enableVibration(severity == "CRITICAL")
                    }
                )
            }
            val runtimePermissionGranted = Build.VERSION.SDK_INT < 33 ||
                context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
            val channelEnabled = Build.VERSION.SDK_INT < Build.VERSION_CODES.O ||
                manager.getNotificationChannel(channelId)?.importance != NotificationManager.IMPORTANCE_NONE
            if (!shouldPostSystemNotification(
                    appNotificationsEnabled = manager.areNotificationsEnabled(),
                    channelEnabled = channelEnabled,
                    runtimePermissionGranted = runtimePermissionGranted,
                )
            ) return false

            val notification = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                Notification.Builder(context, channelId)
            } else {
                @Suppress("DEPRECATION")
                Notification.Builder(context)
            }
            notification
                .setSmallIcon(R.drawable.ic_solo_notification)
                .setContentTitle(title)
                .setContentText(message)
                .setStyle(Notification.BigTextStyle().bigText(message))
                .setCategory(Notification.CATEGORY_STATUS)
                .setAutoCancel(true)
                .setWhen(System.currentTimeMillis())
            manager.notify(eventId.hashCode() and 0x7fffffff, notification.build())
            true
        } catch (_: Exception) {
            false
        }
    }

    /** Posts a harmless local Android notification without creating a risk event or ledger record. */
    internal fun sendTestNotification(context: Context): Boolean = postSystemNotification(
        context = context,
        eventId = "solo-notification-test",
        title = "Solo notification test",
        message = "Android system notification test. No risk event or order was created.",
        severity = "HIGH",
    )

    fun getRecords(context: Context): List<SoloNotificationRecord> {
        readRecords(context)
            .filter { !it.osDelivered && !it.acknowledged && !it.resolved }
            .forEach { record ->
                if (postSystemNotification(context, record.eventId, record.title, record.message, record.severity)) {
                    updateRecord(context, record.eventId) { it.copy(osDelivered = true) }
                }
            }
        return readRecords(context)
    }

    fun acknowledge(context: Context, eventId: String) {
        updateRecord(context, eventId) { it.copy(acknowledged = true) }
    }

    private fun resolveLatest(context: Context, type: String) {
        val records = readRecords(context).toMutableList()
        val index = records.indexOfFirst { it.type == type && !it.resolved }
        if (index >= 0) {
            records[index] = records[index].copy(resolved = true)
            saveRecords(context, records)
        }
    }

    private fun updateRecord(
        context: Context,
        eventId: String,
        transform: (SoloNotificationRecord) -> SoloNotificationRecord,
    ) {
        val records = readRecords(context).map { if (it.eventId == eventId) transform(it) else it }
        saveRecords(context, records)
    }

    private fun readRecords(context: Context): List<SoloNotificationRecord> {
        val raw = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(EVENTS, "[]") ?: "[]"
        return runCatching {
            val array = JSONArray(raw)
            (0 until array.length()).mapNotNull { index ->
                val row = array.optJSONObject(index) ?: return@mapNotNull null
                SoloNotificationRecord(
                    eventId = row.optString("event_id"),
                    type = row.optString("type"),
                    severity = row.optString("severity"),
                    title = row.optString("title"),
                    message = row.optString("message"),
                    occurredAtEpochMs = row.optLong("occurred_at"),
                    acknowledged = row.optBoolean("acknowledged"),
                    resolved = row.optBoolean("resolved"),
                    osDelivered = row.optBoolean("os_delivered"),
                )
            }.filter { it.eventId.isNotBlank() && it.title.isNotBlank() }
        }.getOrDefault(emptyList())
    }

    private fun saveRecords(context: Context, records: List<SoloNotificationRecord>) {
        val array = JSONArray()
        records.take(MAX_EVENTS).forEach { record ->
            array.put(
                JSONObject()
                    .put("event_id", record.eventId)
                    .put("type", record.type)
                    .put("severity", record.severity)
                    .put("title", record.title)
                    .put("message", record.message)
                    .put("occurred_at", record.occurredAtEpochMs)
                    .put("acknowledged", record.acknowledged)
                    .put("resolved", record.resolved)
                    .put("os_delivered", record.osDelivered)
            )
        }
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().putString(EVENTS, array.toString()).apply()
    }
}
