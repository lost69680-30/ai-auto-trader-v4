package com.aiautotrader.v4

internal enum class SoloAlertSeverity {
    CRITICAL, HIGH, WARNING, INFO
}

internal enum class SoloAlertType(val requiredSeverity: SoloAlertSeverity) {
    EMERGENCY_STOP(SoloAlertSeverity.CRITICAL),
    TRADING_PAUSED(SoloAlertSeverity.HIGH),
    DATABASE_FAILURE(SoloAlertSeverity.CRITICAL),
    MARKET_DATA_STALE(SoloAlertSeverity.HIGH),
    RISK_LIMIT_BREACH(SoloAlertSeverity.CRITICAL),
}

internal data class SoloAlertEvent(
    val eventId: String,
    val type: SoloAlertType,
    val conditionKey: String,
    val severity: SoloAlertSeverity = type.requiredSeverity,
    val title: String,
    val message: String,
    val occurredAtEpochMs: Long,
    val source: String,
    val scope: String,
)

internal enum class SoloNotificationDecision {
    ACCEPTED,
    DUPLICATE,
    COOLED_DOWN,
    INVALID,
}

/**
 * Deterministic notification admission policy. This class does not deliver alerts and
 * cannot change runtime, risk, or order authorization state.
 *
 * The caller must supply an authoritative event and a trusted current timestamp.
 */
internal class SoloNotificationGate(
    private val cooldownMillis: Long = 60_000L,
    private val maxRememberedEventIds: Int = 2_048,
) {
    private data class ConditionKey(
        val type: SoloAlertType,
        val scope: String,
        val condition: String,
    )

    private val seenEventIds = LinkedHashMap<String, Unit>()
    private val lastAcceptedAt = mutableMapOf<ConditionKey, Long>()

    init {
        require(cooldownMillis >= 0L) { "cooldown must not be negative" }
        require(maxRememberedEventIds > 0) { "event id capacity must be positive" }
    }

    @Synchronized
    fun evaluate(event: SoloAlertEvent, nowEpochMs: Long): SoloNotificationDecision {
        if (!isValid(event, nowEpochMs)) return SoloNotificationDecision.INVALID
        if (seenEventIds.containsKey(event.eventId)) return SoloNotificationDecision.DUPLICATE

        val key = ConditionKey(event.type, event.scope.trim(), event.conditionKey.trim())
        val previousAcceptedAt = lastAcceptedAt[key]
        if (previousAcceptedAt != null && cooldownMillis > 0L) {
            // A backwards wall-clock jump must not defeat duplicate suppression.
            val elapsed = if (nowEpochMs >= previousAcceptedAt) nowEpochMs - previousAcceptedAt else 0L
            remember(event.eventId)
            if (elapsed < cooldownMillis) return SoloNotificationDecision.COOLED_DOWN
        }

        remember(event.eventId)
        lastAcceptedAt[key] = nowEpochMs
        return SoloNotificationDecision.ACCEPTED
    }

    private fun isValid(event: SoloAlertEvent, nowEpochMs: Long): Boolean {
        if (nowEpochMs < 0L || event.occurredAtEpochMs <= 0L) return false
        if (event.occurredAtEpochMs > nowEpochMs + MAX_FUTURE_SKEW_MS) return false
        if (event.eventId.isBlank() || event.eventId.length > 128) return false
        if (event.conditionKey.isBlank() || event.conditionKey.length > 160) return false
        if (event.title.isBlank() || event.title.length > 120) return false
        if (event.message.isBlank() || event.message.length > 500) return false
        if (event.source.isBlank() || event.source.length > 128) return false
        if (event.scope.isBlank() || event.scope.length > 128) return false
        if (event.severity != event.type.requiredSeverity) return false
        return true
    }

    private fun remember(eventId: String) {
        seenEventIds[eventId] = Unit
        while (seenEventIds.size > maxRememberedEventIds) {
            val oldest = seenEventIds.keys.firstOrNull() ?: break
            seenEventIds.remove(oldest)
        }
    }

    private companion object {
        const val MAX_FUTURE_SKEW_MS = 60_000L
    }
}

/**
 * Builds and admits an alert event at the notification publisher boundary.
 * Severity is derived from the event type, never supplied by an upstream caller.
 */
internal fun evaluateSoloAlertAdmission(
    gate: SoloNotificationGate,
    eventId: String,
    type: SoloAlertType,
    conditionKey: String,
    title: String,
    message: String,
    occurredAtEpochMs: Long,
    nowEpochMs: Long,
    source: String,
    scope: String,
): SoloNotificationDecision = gate.evaluate(
    SoloAlertEvent(
        eventId = eventId,
        type = type,
        conditionKey = conditionKey,
        title = title,
        message = message,
        occurredAtEpochMs = occurredAtEpochMs,
        source = source,
        scope = scope,
    ),
    nowEpochMs,
)


/** Paper-ledger alerts are observed only with an authenticated foreground session. */
internal fun shouldRefreshPaperLedger(sessionToken: String, isForeground: Boolean): Boolean =
    sessionToken.length >= 32 && isForeground
