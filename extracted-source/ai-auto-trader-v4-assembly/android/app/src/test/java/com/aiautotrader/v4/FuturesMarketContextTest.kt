package com.aiautotrader.v4

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test
import java.time.Instant

class FuturesMarketContextTest {
    private val now = Instant.parse("2026-10-30T03:00:00Z")
    private val nextFunding = "2026-10-30T04:00:00+00:00"

    @Test
    fun acceptsCompleteFuturesMarkIndexAndFundingContext() {
        val context = validateMarketDataContext(
            marketType = "FUTURES",
            priceType = "FUTURES_MARK_PRICE",
            indexPrice = 67000.0,
            fundingRate = 0.0001,
            nextFundingAt = nextFunding,
            now = now,
        )

        assertNotNull(context)
        assertEquals("FUTURES", context!!.marketType)
        assertEquals(67000.0, context.indexPrice!!, 0.0)
        assertEquals(0.0001, context.fundingRate!!, 0.0)
        assertEquals(nextFunding, context.nextFundingAt)
    }

    @Test
    fun acceptsSpotOnlyWhenFuturesFieldsAreAbsent() {
        val context = validateMarketDataContext(
            marketType = "SPOT",
            priceType = "SPOT_LAST_PRICE",
            indexPrice = null,
            fundingRate = null,
            nextFundingAt = null,
            now = now,
        )

        assertNotNull(context)
        assertNull(context!!.indexPrice)
        assertNull(context.fundingRate)
        assertNull(context.nextFundingAt)
    }

    @Test
    fun rejectsFuturesRowsWithMissingOrInvalidContext() {
        assertNull(validateMarketDataContext("FUTURES", "FUTURES_MARK_PRICE", 67000.0, null, nextFunding, now))
        assertNull(validateMarketDataContext("FUTURES", "FUTURES_MARK_PRICE", Double.NaN, 0.0001, nextFunding, now))
        assertNull(validateMarketDataContext("FUTURES", "FUTURES_MARK_PRICE", 0.0, 0.0001, nextFunding, now))
        assertNull(validateMarketDataContext("FUTURES", "SPOT_LAST_PRICE", 67000.0, 0.0001, nextFunding, now))
    }

    @Test
    fun rejectsStaleOrMalformedNextFundingTimestamp() {
        assertNull(validateMarketDataContext("FUTURES", "FUTURES_MARK_PRICE", 67000.0, 0.0001, "not-a-time", now))
        assertNull(validateMarketDataContext("FUTURES", "FUTURES_MARK_PRICE", 67000.0, 0.0001, "2026-10-30T02:00:00Z", now))
    }

    @Test
    fun formatsFundingRateAsPercentWithoutHidingItsSign() {
        assertEquals("+0.0100%", formatFundingRate(0.0001))
        assertEquals("-0.0250%", formatFundingRate(-0.00025))
        assertEquals("Unavailable", formatFundingRate(null))
        assertEquals("Unavailable", formatFundingRate(Double.NaN))
    }

    @Test
    fun formatsNextFundingTimeInUtc() {
        assertEquals("30 Oct 2026, 04:00 UTC", formatFundingTimestamp(nextFunding))
        assertEquals("Unavailable", formatFundingTimestamp("invalid"))
    }
}
