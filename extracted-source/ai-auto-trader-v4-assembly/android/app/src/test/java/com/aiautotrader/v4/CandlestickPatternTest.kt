package com.aiautotrader.v4

import org.junit.Assert.assertEquals
import org.junit.Test

class CandlestickPatternTest {
    private fun candle(open: Double, high: Double, low: Double, close: Double) =
        MarketCandle(
            openTime = "2026-10-01T00:00:00Z",
            open = open,
            high = high,
            low = low,
            close = close,
            volume = 100.0,
        )

    @Test
    fun requiresTwoCandlesBeforeNamingAPattern() {
        assertEquals(
            "Insufficient history · 2 candles required",
            detectRecentCandlestickPattern(listOf(candle(10.0, 11.0, 9.0, 10.5))),
        )
    }

    @Test
    fun recognizesBullishEngulfingAsCandidateNotTradeSignal() {
        val candles = listOf(
            candle(10.0, 10.2, 8.8, 9.0),
            candle(8.9, 10.5, 8.7, 10.2),
        )
        val result = detectRecentCandlestickPattern(candles)
        assertEquals("Bullish engulfing candidate · context required", result)
    }

    @Test
    fun recognizesBearishEngulfingAsCandidateNotTradeSignal() {
        val candles = listOf(
            candle(9.0, 10.3, 8.8, 10.0),
            candle(10.2, 10.4, 8.5, 8.8),
        )
        val result = detectRecentCandlestickPattern(candles)
        assertEquals("Bearish engulfing candidate · context required", result)
    }

    @Test
    fun recognizesDojiWithoutClaimingDirection() {
        val result = detectRecentCandlestickPattern(
            listOf(candle(10.0, 10.01, 9.99, 10.001), candle(10.0, 10.5, 9.5, 10.02)),
        )
        assertEquals("Doji-like candle · direction unconfirmed", result)
    }

    @Test
    fun recognizesHammerLikeShapeButRequiresTrendContext() {
        val result = detectRecentCandlestickPattern(
            listOf(candle(10.0, 10.2, 9.8, 10.1), candle(10.0, 10.3, 8.8, 10.2)),
        )
        assertEquals("Hammer-like candle · context required", result)
    }

    @Test
    fun reportsNoDefinedPatternWhenShapeDoesNotMatch() {
        val result = detectRecentCandlestickPattern(
            listOf(candle(10.0, 10.3, 9.7, 10.1), candle(10.0, 10.5, 9.5, 10.2)),
        )
        assertEquals("No defined 2-candle pattern detected", result)
    }

    @Test
    fun rejectsMalformedCandlesRatherThanLabelingThem() {
        val result = detectRecentCandlestickPattern(
            listOf(candle(10.0, 10.3, 9.7, 10.1), candle(10.0, 9.5, 9.8, 10.2)),
        )
        assertEquals("Blocked · invalid candle data", result)
    }
}