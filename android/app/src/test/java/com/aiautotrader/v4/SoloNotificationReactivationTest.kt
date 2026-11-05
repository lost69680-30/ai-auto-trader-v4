package com.aiautotrader.v4

import android.Manifest
import android.app.NotificationManager
import android.content.Context
import android.content.ContextWrapper
import android.content.pm.PackageManager
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class SoloNotificationReactivationTest {
    private lateinit var context: PermissionOverrideContext
    private lateinit var manager: NotificationManager

    @Before
    fun setUp() {
        val base = RuntimeEnvironment.getApplication()
        context = PermissionOverrideContext(base)
        base.getSharedPreferences("solo_notification_center", Context.MODE_PRIVATE)
            .edit().clear().commit()
        manager = base.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        manager.cancelAll()
    }

    @Test
    fun newActivationAfterVerifiedRecoveryCreatesANewAlertWithinCooldown() {
        SoloNotificationCenter.observeVerifiedRiskSnapshot(context, risk(emergencyStopActive = true))
        val first = SoloNotificationCenter.getRecords(context).single()

        SoloNotificationCenter.observeVerifiedRiskSnapshot(context, risk(emergencyStopActive = false))
        Thread.sleep(5)
        SoloNotificationCenter.observeVerifiedRiskSnapshot(context, risk(emergencyStopActive = true))

        val records = SoloNotificationCenter.getRecords(context)
        assertEquals(2, records.size)
        assertNotEquals(first.eventId, records.first().eventId)
        assertFalse(records.first().resolved)
        assertTrue(records.last().resolved)
        assertEquals(2, manager.activeNotifications.size)
    }

    private class PermissionOverrideContext(base: Context) : ContextWrapper(base) {
        override fun checkSelfPermission(permission: String): Int =
            PackageManager.PERMISSION_GRANTED
    }

    private fun risk(emergencyStopActive: Boolean) = PaperRiskView(
        emergencyStopActive = emergencyStopActive,
        openPositionCount = 0,
        accountEquity = 1_000.0,
        drawdownAmount = 0.0,
        drawdownPct = 0.0,
        unrealizedPnl = 0.0,
        marksFresh = true,
        snapshotAt = "2026-11-05T00:00:00Z",
        stateStorage = "POSTGRESQL",
    )
}
