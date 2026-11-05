package com.aiautotrader.v4

import org.junit.Assert.assertEquals
import org.junit.Test

class SoloScreenNavigationTest {
    @Test fun marketNavigationOpensChartsAndAnalysis() {
        assertEquals("Charts & Analysis", soloScreenDestination("Markets"))
    }

    @Test fun otherCoreDestinationsRemainStable() {
        listOf("Dashboard", "AI Agents", "Opportunities", "Positions", "Risk Center",
            "Performance", "Notifications", "Security", "Settings").forEach { screen ->
            assertEquals(screen, soloScreenDestination(screen))
        }
    }
}
