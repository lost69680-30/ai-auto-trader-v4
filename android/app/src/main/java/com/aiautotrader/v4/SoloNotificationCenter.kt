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

internal object SoloNotificationCenter {
    private const val PREFS = "solo_notification_center"
    private const val EVENTS = "events"
    private const val LAST_STOP_STATE = "last_emergency_stop_state"
    private const val LAST_MARKS_STATE = "last_marks_fresh_state"
    private const val CHANNEL_ID = "solo_critical_alerts"
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
        val previous = prefs.getString(preferenceKey, null)
        val state = if (current) "ACTIVE" else "INACTIVE"
        if (previous == state) return
        prefs.edit().putString(preferenceKey, state).apply()
        if (current) {
            publish(context, type, severity, title, message)
        } else if (previous == "ACTIVE") {
            resolveLatest(context, type)
        }
    }

    private fun publish(
        context: Context,
        type: String,
        severity: String,
        title: String,
        message: String,
    ) {
        val now = System.currentTimeMillis()
        val eventId = "$type:$now"
        val osDelivered = postSystemNotification(context, eventId, title, message, severity)
        val records = readRecords(context).toMutableList()
        records.add(
            0,
            SoloNotificationRecord(
                eventId, type, severity, title, message, now,
                acknowledged = false, resolved = false, osDelivered = osDelivered,
            )
        )
        saveRecords(context, records.take(MAX_EVENTS))
    }

    private fun postSystemNotification(
        context: Context,
        eventId: String,
        title: String,
        message: String,
        severity: String,
    ): Boolean {
        val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            manager.createNotificationChannel(
                NotificationChannel(
                    CHANNEL_ID,
                    "Solo critical alerts",
                    if (severity == "CRITICAL") NotificationManager.IMPORTANCE_HIGH
                    else NotificationManager.IMPORTANCE_DEFAULT,
                ).apply {
                    description = "Verified Solo risk and market-data alerts"
                    enableVibration(severity == "CRITICAL")
                }
            )
        }
        if (Build.VERSION.SDK_INT >= 33 &&
            context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) return false

        return try {
            val notification = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                Notification.Builder(context, CHANNEL_ID)
            } else {
                @Suppress("DEPRECATION")
                Notification.Builder(context)
            }
            notification
                .setSmallIcon(android.R.drawable.ic_dialog_alert)
                .setContentTitle(title)
                .setContentText(message)
                .setStyle(Notification.BigTextStyle().bigText(message))
                .setCategory(Notification.CATEGORY_STATUS)
                .setAutoCancel(true)
                .setWhen(System.currentTimeMillis())
            manager.notify(eventId.hashCode() and 0x7fffffff, notification.build())
            true
        } catch (_: SecurityException) {
            false
        }
    }

    fun getRecords(context: Context): List<SoloNotificationRecord> = readRecords(context)

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
