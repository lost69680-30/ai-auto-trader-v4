package com.aiautotrader.v4

import android.Manifest
import android.app.Application
import android.app.NotificationManager
import android.content.Context
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class SoloNotificationRuntimeTest {
    private lateinit var application: Application
    private lateinit var context: Context
    private lateinit var manager: NotificationManager

    @Before
    fun setUp() {
        application = RuntimeEnvironment.getApplication()
        context = application
        context.getSharedPreferences("solo_notification_center", Context.MODE_PRIVATE)
            .edit().clear().commit()
        manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        manager.cancelAll()
        shadowOf(application).grantPermissions(Manifest.permission.POST_NOTIFICATIONS)
    }

    @Test
    fun verifiedRiskAlertIsPersistedAndRetriedAfterNotificationPermissionReturns() {
        shadowOf(context).denyPermissions(Manifest.permission.POST_NOTIFICATIONS)

        SoloNotificationCenter.observeVerifiedRiskSnapshot(context, risk(emergencyStopActive = true))
        var records = SoloNotificationCenter.getRecords(context)

        assertEquals(1, records.size)
        assertFalse(records.single().osDelivered)
        assertEquals(0, manager.activeNotifications.size)

        shadowOf(context).grantPermissions(Manifest.permission.POST_NOTIFICATIONS)
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
