package com.aiautotrader.v4

import android.Manifest
import android.app.NotificationManager
import android.content.Context
import android.content.ContextWrapper
import android.content.pm.PackageManager
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33])
class SoloNotificationRuntimeTest {
    private lateinit var baseContext: Context
    private lateinit var context: PermissionOverrideContext
    private lateinit var manager: NotificationManager

    @Before
    fun setUp() {
        baseContext = RuntimeEnvironment.getApplication()
        context = PermissionOverrideContext(baseContext)
        baseContext.getSharedPreferences("solo_notification_center", Context.MODE_PRIVATE)
            .edit().clear().commit()
        manager = baseContext.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        manager.cancelAll()
        context.notificationPermissionGranted = true
    }

    @Test
    fun verifiedRiskAlertIsPersistedAndRetriedAfterNotificationPermissionReturns() {
        context.notificationPermissionGranted = false

        SoloNotificationCenter.observeVerifiedRiskSnapshot(context, risk(emergencyStopActive = true))
        var records = SoloNotificationCenter.getRecords(context)

        assertEquals(1, records.size)
        assertFalse(records.single().osDelivered)
        assertEquals(0, manager.activeNotifications.size)

        context.notificationPermissionGranted = true
        records = SoloNotificationCenter.getRecords(context)

        assertTrue(records.single().osDelivered)
        assertEquals(1, manager.activeNotifications.size)

        SoloNotificationCenter.acknowledge(context, records.single().eventId)
        assertTrue(SoloNotificationCenter.getRecords(context).single().acknowledged)

        SoloNotificationCenter.observeVerifiedRiskSnapshot(context, risk(emergencyStopActive = false))
        records = SoloNotificationCenter.getRecords(context)
        assertTrue(records.single().resolved)
        assertTrue(records.single().acknowledged)
        assertEquals(1, manager.activeNotifications.size)
    }

    @Test
    fun initialInactiveRiskSnapshotDoesNotCreateANotification() {
        SoloNotificationCenter.observeVerifiedRiskSnapshot(context, risk(emergencyStopActive = false))

        assertTrue(SoloNotificationCenter.getRecords(context).isEmpty())
        assertEquals(0, manager.activeNotifications.size)
    }

    private class PermissionOverrideContext(base: Context) : ContextWrapper(base) {
        var notificationPermissionGranted: Boolean = true

        override fun checkSelfPermission(permission: String): Int =
            if (permission != Manifest.permission.POST_NOTIFICATIONS || notificationPermissionGranted) {
                PackageManager.PERMISSION_GRANTED
            } else {
                PackageManager.PERMISSION_DENIED
            }
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
