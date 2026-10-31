package com.aiautotrader.v4

import java.time.Instant
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class MarketObservationFreshnessTest {
    private val now = Instant.parse("2026-10-30T12:00:00Z")

    @Test
    fun acceptsFreshExchangeObservation() {
        assertEquals(Instant.parse("2026-10-30T11:59:30Z"), parseFreshMarketObservation("2026-10-30T11:59:30Z", now))
    }

    @Test
    fun rejectsMissingMalformedAndStaleObservations() {
        assertNull(parseFreshMarketObservation(null, now))
        assertNull(parseFreshMarketObservation("not-a-timestamp", now))
        assertNull(parseFreshMarketObservation("2026-10-30T11:58:29Z", now))
    }

    @Test
    fun rejectsObservationsMoreThanOneSecondInTheFuture() {
        assertNull(parseFreshMarketObservation("2026-10-30T12:00:02Z", now))
    }

    @Test
    fun acceptsObservationAtTheConfiguredAgeBoundary() {
        assertEquals(Instant.parse("2026-10-30T11:58:30Z"), parseFreshMarketObservation("2026-10-30T11:58:30Z", now))
    }

    @Test
    fun rejectsInvalidMaximumAgeConfiguration() {
        assertNull(parseFreshMarketObservation("2026-10-30T11:59:30Z", now, 0.0))
        assertNull(parseFreshMarketObservation("2026-10-30T11:59:30Z", now, Double.NaN))
    }
}

    @Test
    fun labelsOnlyFreshConnectedStreamAsLive() {
        val timestamp = "2026-10-30T11:59:30Z"
        assertEquals(MarketDisplayFreshness.LIVE,
            classifyMarketDisplayFreshness(timestamp, "CONNECTED", now))
        assertEquals(MarketDisplayFreshness.RECENT_DISCONNECTED,
            classifyMarketDisplayFreshness(timestamp, "RECONNECTING", now))
    }

    @Test
    fun distinguishesStaleAndUnavailableSnapshots() {
        assertEquals(MarketDisplayFreshness.STALE,
            classifyMarketDisplayFreshness("2026-10-30T11:58:29Z", "CONNECTED", now))
        assertEquals(MarketDisplayFreshness.UNAVAILABLE,
            classifyMarketDisplayFreshness(null, "CONNECTED", now))
    }
}
