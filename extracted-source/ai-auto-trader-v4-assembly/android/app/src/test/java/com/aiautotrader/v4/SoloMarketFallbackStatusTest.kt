package com.aiautotrader.v4

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SoloMarketFallbackStatusTest {
    @Test fun acceptsOnlyExplicitPaperOnlyMarketFallbackStatus() {
        assertTrue(isMarketOnlyFallbackStatus("DEGRADED_MARKET_ONLY", "PAPER_ONLY", false))
    }

    @Test fun rejectsFallbackStatusWhenAuthorizationIsUnknownOrEnabled() {
        assertFalse(isMarketOnlyFallbackStatus("DEGRADED_MARKET_ONLY", "PAPER_ONLY", true))
        assertFalse(isMarketOnlyFallbackStatus("READY", "PAPER_ONLY", false))
        assertFalse(isMarketOnlyFallbackStatus("DEGRADED_MARKET_ONLY", "LIVE", false))
        assertFalse(isMarketOnlyFallbackStatus("", "PAPER_ONLY", false))
        assertFalse(isMarketOnlyFallbackStatus("UNKNOWN", "PAPER_ONLY", false))
        assertFalse(isMarketOnlyFallbackStatus("DEGRADED_MARKET_ONLY", "", false))
    }

    @Test fun displaysOnlyValidatedDegradedSnapshotsFromExplicitFallback() {
        assertTrue(shouldDisplayMarketSnapshot(false, true, true, "DEGRADED", true))
        assertTrue(shouldDisplayMarketSnapshot(true, true, true, "DEGRADED", true))
        assertFalse(shouldDisplayMarketSnapshot(false, true, false, "DEGRADED", true))
        assertFalse(shouldDisplayMarketSnapshot(false, true, true, "DEGRADED", false))
        assertFalse(shouldDisplayMarketSnapshot(false, false, true, "DEGRADED", true))
        assertTrue(shouldDisplayMarketSnapshot(true, false, true, "HEALTHY", true))
        assertFalse(shouldDisplayMarketSnapshot(true, false, true, "DEGRADED", true))
    }
}
