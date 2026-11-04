package com.aiautotrader.v4

import org.json.JSONObject
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SoloMarketFallbackStatusTest {
    @Test fun acceptsOnlyExplicitPaperOnlyMarketFallbackStatus() {
        val status = JSONObject("""{"status":"DEGRADED_MARKET_ONLY","mode":"PAPER_ONLY","orders_authorized":false}""")
        assertTrue(isMarketOnlyFallbackStatus(status))
    }

    @Test fun rejectsFallbackStatusWhenAuthorizationIsUnknownOrEnabled() {
        assertFalse(isMarketOnlyFallbackStatus(JSONObject("""{"status":"DEGRADED_MARKET_ONLY","mode":"PAPER_ONLY"}""")))
        assertFalse(isMarketOnlyFallbackStatus(JSONObject("""{"status":"DEGRADED_MARKET_ONLY","mode":"PAPER_ONLY","orders_authorized":true}""")))
        assertFalse(isMarketOnlyFallbackStatus(JSONObject("""{"status":"READY","mode":"PAPER_ONLY","orders_authorized":false}""")))
    }

    @Test fun displaysOnlyValidatedDegradedSnapshotsFromExplicitFallback() {
        assertTrue(shouldDisplayMarketSnapshot(false, true, true, "DEGRADED", true))
        assertFalse(shouldDisplayMarketSnapshot(false, true, false, "DEGRADED", true))
        assertFalse(shouldDisplayMarketSnapshot(false, true, true, "DEGRADED", false))
        assertFalse(shouldDisplayMarketSnapshot(false, false, true, "DEGRADED", true))
        assertTrue(shouldDisplayMarketSnapshot(true, false, true, "HEALTHY", true))
        assertFalse(shouldDisplayMarketSnapshot(true, false, true, "DEGRADED", true))
    }
}
