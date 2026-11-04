package com.aiautotrader.v4

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Test

class SoloNotificationPolicyTest {
    private fun event(
        id: String,
        type: SoloAlertType = SoloAlertType.EMERGENCY_STOP,
        condition: String = "stop-active",
        message: String = "Emergency Stop is active. New orders are blocked.",
    ) = SoloAlertEvent(
        eventId = id,
        type = type,
        conditionKey = condition,
        title = "Emergency Stop active",
        message = message,
        occurredAtEpochMs = 1_000L,
        source = "risk_guardian",
        scope = "account",
    )

    @Test fun acceptsFirstValidCriticalEvent() {
        val gate = SoloNotificationGate(cooldownMillis = 60_000L)
        assertEquals(SoloNotificationDecision.ACCEPTED, gate.evaluate(event("evt-1"), 1_000L))
    }

    @Test fun rejectsDuplicateEventIdAfterCooldown() {
        val gate = SoloNotificationGate(cooldownMillis = 100L)
        assertEquals(SoloNotificationDecision.ACCEPTED, gate.evaluate(event("evt-1"), 1_000L))
        assertEquals(SoloNotificationDecision.DUPLICATE, gate.evaluate(event("evt-1"), 2_000L))
    }

    @Test fun coalescesRepeatedSameConditionDuringCooldown() {
        val gate = SoloNotificationGate(cooldownMillis = 60_000L)
        assertEquals(SoloNotificationDecision.ACCEPTED, gate.evaluate(event("evt-1"), 1_000L))
        assertEquals(SoloNotificationDecision.COOLED_DOWN, gate.evaluate(event("evt-2"), 2_000L))
    }

    @Test fun doesNotSuppressMateriallyChangedCondition() {
        val gate = SoloNotificationGate(cooldownMillis = 60_000L)
        assertEquals(SoloNotificationDecision.ACCEPTED, gate.evaluate(event("evt-1"), 1_000L))
        assertEquals(
            SoloNotificationDecision.ACCEPTED,
            gate.evaluate(event("evt-2", condition = "stop-cleared-then-reenabled"), 2_000L),
        )
    }

    @Test fun separateEventTypesHaveIndependentCooldowns() {
        val gate = SoloNotificationGate(cooldownMillis = 60_000L)
        assertEquals(SoloNotificationDecision.ACCEPTED, gate.evaluate(event("evt-1"), 1_000L))
        assertEquals(
            SoloNotificationDecision.ACCEPTED,
            gate.evaluate(event("evt-2", type = SoloAlertType.DATABASE_FAILURE, condition = "db-down"), 2_000L),
        )
    }

    @Test fun rejectsMalformedEventWithoutRecordingIt() {
        val gate = SoloNotificationGate(cooldownMillis = 60_000L)
        val invalid = event(" ", message = " ")
        assertEquals(SoloNotificationDecision.INVALID, gate.evaluate(invalid, 1_000L))
        assertEquals(SoloNotificationDecision.ACCEPTED, gate.evaluate(event("evt-1"), 1_000L))
    }

    @Test fun criticalSeverityIsFixedByEventType() {
        val gate = SoloNotificationGate(cooldownMillis = 60_000L)
        val wrongSeverity = event("evt-1").copy(severity = SoloAlertSeverity.INFO)
        assertEquals(SoloNotificationDecision.INVALID, gate.evaluate(wrongSeverity, 1_000L))
        assertNotNull(SoloAlertType.EMERGENCY_STOP.requiredSeverity)
        assertEquals(SoloAlertSeverity.CRITICAL, SoloAlertType.EMERGENCY_STOP.requiredSeverity)
    }
}
