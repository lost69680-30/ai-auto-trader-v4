package com.aiautotrader.v4

import org.junit.Assert.assertEquals
import org.junit.Test

class TimeframeBiasTest {
    @Test
    fun yearlyHistoryCanUseCoreTrendAndMomentumWithoutEma50() {
        assertEquals(
            "BULLISH",
            classifyTimeframeBias(
                trend = "Above EMA20 · EMA50 unavailable",
                momentum = "Positive (61.0)",
                directionalReady = true,
            ),
        )
    }

    @Test
    fun yearlyHistoryCanShowBearishBiasWithoutEma50() {
        assertEquals(
            "BEARISH",
            classifyTimeframeBias(
                trend = "Below EMA20 · EMA50 unavailable",
                momentum = "Negative (39.0)",
                directionalReady = true,
            ),
        )
    }

    @Test
    fun insufficientDataNeverProducesDirectionalBias() {
        assertEquals(
            "INSUFFICIENT DATA",
            classifyTimeframeBias(
                trend = "Above EMA20 · EMA50 unavailable",
                momentum = "Positive (61.0)",
                directionalReady = false,
            ),
        )
    }

    @Test
    fun conflictingTrendAndMomentumRemainNeutral() {
        assertEquals(
            "NEUTRAL / MIXED",
            classifyTimeframeBias(
                trend = "Above EMA20 · EMA50 unavailable",
                momentum = "Negative (39.0)",
                directionalReady = true,
            ),
        )
    }

    @Test
    fun fullEmaAlignmentStillProducesBullishBias() {
        assertEquals(
            "BULLISH",
            classifyTimeframeBias(
                trend = "Uptrend · price > EMA20 > EMA50",
                momentum = "Positive (61.0)",
                directionalReady = true,
            ),
        )
    }
}
