package com.aiautotrader.v4

import java.time.Instant
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class MarketAnalysisChecksTest {
    private fun candle(time: String, volume: Double = 100.0) = MarketCandle(
        openTime = time, open = 100.0, high = 101.0, low = 99.0,
        close = 100.5, volume = volume
    )

    @Test
    fun acceptsContiguousFreshMinuteCandles() {
        val candles = listOf(
            candle("2026-10-28T08:58:00Z"),
            candle("2026-10-28T08:59:00Z")
        )
        assertNull(candleSeriesIssue("1m", candles, Instant.parse("2026-10-28T09:01:00Z")))
    }

    @Test
    fun blocksMissingDuplicateAndOutOfOrderBars() {
        val missing = listOf(candle("2026-10-28T08:57:00Z"), candle("2026-10-28T08:59:00Z"))
        assertTrue(candleSeriesIssue("1m", missing, Instant.parse("2026-10-28T09:01:00Z")).orEmpty().contains("gap"))

        val duplicate = listOf(candle("2026-10-28T08:59:00Z"), candle("2026-10-28T08:59:00Z"))
        assertTrue(candleSeriesIssue("1m", duplicate, Instant.parse("2026-10-28T09:01:00Z")).orEmpty().contains("duplicate"))

        val reversed = listOf(candle("2026-10-28T08:59:00Z"), candle("2026-10-28T08:58:00Z"))
        assertTrue(candleSeriesIssue("1m", reversed, Instant.parse("2026-10-28T09:01:00Z")).orEmpty().contains("duplicate"))
    }

    @Test
    fun blocksStaleAndNotYetClosedHistory() {
        val stale = listOf(candle("2026-10-25T00:00:00Z"))
        assertEquals("latest candle is stale", candleSeriesIssue("1h", stale, Instant.parse("2026-10-28T00:00:00Z")))

        val notClosed = listOf(candle("2026-10-28T09:00:00Z"))
        assertEquals("latest candle is not yet closed", candleSeriesIssue("1h", notClosed, Instant.parse("2026-10-28T09:30:00Z")))
    }

    @Test
    fun requiresConsecutiveCalendarMonthsAndCompleteYearBoundaries() {
        val months = listOf(candle("2026-08-01T00:00:00Z"), candle("2026-09-01T00:00:00Z"))
        assertNull(candleSeriesIssue("1M", months, Instant.parse("2026-10-28T00:00:00Z")))
        val missingMonth = listOf(candle("2026-08-01T00:00:00Z"), candle("2026-10-01T00:00:00Z"))
        assertTrue(candleSeriesIssue("1M", missingMonth, Instant.parse("2026-11-01T00:00:00Z")).orEmpty().contains("gap"))

        val years = listOf(candle("2024-01-01T00:00:00Z"), candle("2025-01-01T00:00:00Z"))
        assertNull(candleSeriesIssue("1Y", years, Instant.parse("2026-10-28T00:00:00Z")))
        val currentYear = listOf(candle("2026-01-01T00:00:00Z"))
        assertEquals("latest candle is not yet closed", candleSeriesIssue("1Y", currentYear, Instant.parse("2026-10-28T00:00:00Z")))
    }

    @Test
    fun relativeVolumeUsesOnlyThePreviousTwentyBars() {
        val previous = (0 until 20).map { i ->
            candle(Instant.parse("2026-10-28T08:00:00Z").plusSeconds(i * 60L).toString(), 100.0)
        }
        val candles = previous + candle("2026-10-28T08:20:00Z", 200.0)
        assertEquals(2.0, relativeVolumeRatio(candles)!!, 0.000001)
        assertNull(relativeVolumeRatio(candles.take(20)))
    }

    @Test
    fun invalidOHLCVIsRejectedWithoutFilteringItAway() {
        val malformed = MarketCandle("2026-10-28T08:59:00Z", 100.0, 98.0, 99.0, 100.5, 100.0)
        assertEquals("invalid OHLCV values", candleSeriesIssue("1m", listOf(malformed), Instant.parse("2026-10-28T09:01:00Z")))
        val zeroVolume = List(21) { index -> candle(Instant.parse("2026-10-28T08:00:00Z").plusSeconds(index * 60L).toString(), 0.0) }
        assertNull(relativeVolumeRatio(zeroVolume))
    }
}
