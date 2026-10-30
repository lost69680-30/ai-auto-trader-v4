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

    @Test
    fun identifiesConfirmedHigherHighsAndHigherLows() {
        val highs = listOf(11.0, 13.0, 15.0, 13.0, 12.0, 14.0, 16.0, 15.0, 14.0, 15.0, 17.0, 16.0, 15.0)
        val lows = listOf(9.0, 10.0, 12.0, 11.0, 10.0, 11.0, 12.0, 12.0, 11.0, 12.0, 13.0, 14.0, 13.0)
        val candles = highs.indices.map { i ->
            val midpoint = (highs[i] + lows[i]) / 2.0
            MarketCandle("2026-10-28T00:00:00Z", midpoint, highs[i], lows[i], midpoint, 100.0)
        }
        assertTrue(priceActionStructure(candles).contains("higher highs + higher lows"))
    }

    @Test
    fun identifiesConfirmedLowerHighsAndLowerLows() {
        val baseHighs = listOf(11.0, 13.0, 15.0, 13.0, 12.0, 14.0, 16.0, 15.0, 14.0, 15.0, 17.0, 16.0, 15.0)
        val baseLows = listOf(9.0, 10.0, 12.0, 11.0, 10.0, 11.0, 12.0, 12.0, 11.0, 12.0, 13.0, 14.0, 13.0)
        val highs = baseLows.map { 40.0 - it }
        val lows = baseHighs.map { 40.0 - it }
        val candles = highs.indices.map { i ->
            val midpoint = (highs[i] + lows[i]) / 2.0
            MarketCandle("2026-10-28T00:00:00Z", midpoint, highs[i], lows[i], midpoint, 100.0)
        }
        assertTrue(priceActionStructure(candles).contains("lower highs + lower lows"))
    }


    @Test
    fun marketRiskFlagsSurfaceRangeStretchVolumeAndTrendDisagreement() {
        val flags = marketRiskFlags(
            atrPercent = 3.1,
            rsi = 75.0,
            relativeVolume = 2.4,
            trend = "Downtrend · price < EMA20 < EMA50"
        )
        assertTrue(flags.any { it.contains("ATR14") })
        assertTrue(flags.any { it.contains("RSI") && it.contains("70") })
        assertTrue(flags.any { it.contains("volume") && it.contains("2.0") })
        assertTrue(flags.any { it.contains("trend", ignoreCase = true) && it.contains("disagree") })
    }

    @Test
    fun marketRiskFlagsReportUnavailableChecksInsteadOfImplyingLowRisk() {
        val flags = marketRiskFlags(
            atrPercent = null,
            rsi = null,
            relativeVolume = null,
            trend = "Unavailable"
        )
        assertTrue(flags.any { it.contains("ATR check unavailable") })
        assertTrue(flags.any { it.contains("RSI check unavailable") })
        assertTrue(flags.any { it.contains("volume check unavailable") })
        assertTrue(flags.none { it.contains("LOW RISK") })
    }


    @Test
    fun blocksFixedIntervalBarsShiftedAwayFromExchangeBoundaries() {
        val shiftedMinutes = listOf(
            candle("2026-10-28T08:58:30Z"),
            candle("2026-10-28T08:59:30Z")
        )
        assertTrue(
            candleSeriesIssue("1m", shiftedMinutes, Instant.parse("2026-10-28T09:01:00Z"))
                .orEmpty().contains("not aligned")
        )

        val sundayWeeklyBar = listOf(candle("2026-10-25T00:00:00Z"))
        assertTrue(
            candleSeriesIssue("1W", sundayWeeklyBar, Instant.parse("2026-11-03T00:00:00Z"))
                .orEmpty().contains("Monday")
        )

        val shiftedDailyBar = listOf(candle("2026-10-28T00:01:00Z"))
        assertTrue(
            candleSeriesIssue("1D", shiftedDailyBar, Instant.parse("2026-10-29T12:00:00Z"))
                .orEmpty().contains("not aligned")
        )
    }

}
