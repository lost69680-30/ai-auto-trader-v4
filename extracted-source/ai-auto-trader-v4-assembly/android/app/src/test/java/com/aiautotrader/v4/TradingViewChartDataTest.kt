package com.aiautotrader.v4

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class TradingViewChartDataTest {
    @Test
    fun convertsValidatedCandlesToUtcEpochSeconds() {
        val rows = listOf(
            TradingViewOhlc("2026-11-04T10:00:00Z", 100.0, 105.0, 99.0, 103.0, 12.0),
            TradingViewOhlc("2026-11-04T10:01:00Z", 103.0, 106.0, 102.0, 105.0, 14.0),
        )

        val result = toTradingViewCandlestickData(rows)

        assertEquals(2, result.size)
        assertEquals(1_793_784_000L, (result[0].time as com.tradingview.lightweightcharts.api.series.models.Time.Utc).timestamp)
        assertEquals(100f, result[0].open)
        assertEquals(105f, result[1].close)
    }

    @Test
    fun rejectsMalformedAndNonMonotonicCandleHistory() {
        assertThrows(IllegalArgumentException::class.java) {
            toTradingViewCandlestickData(listOf(
                TradingViewOhlc("not-a-time", 100.0, 105.0, 99.0, 103.0, 1.0)
            ))
        }
        assertThrows(IllegalArgumentException::class.java) {
            toTradingViewCandlestickData(listOf(
                TradingViewOhlc("2026-11-04T10:01:00Z", 100.0, 105.0, 99.0, 103.0, 1.0),
                TradingViewOhlc("2026-11-04T10:00:00Z", 103.0, 106.0, 102.0, 105.0, 1.0),
            ))
        }
    }

    @Test
    fun rejectsInvalidOhlcAndNonFiniteValues() {
        assertThrows(IllegalArgumentException::class.java) {
            toTradingViewCandlestickData(listOf(
                TradingViewOhlc("2026-11-04T10:00:00Z", 100.0, 98.0, 99.0, 103.0, 1.0)
            ))
        }
        assertThrows(IllegalArgumentException::class.java) {
            toTradingViewCandlestickData(listOf(
                TradingViewOhlc("2026-11-04T10:00:00Z", 100.0, 105.0, 99.0, Double.NaN, 1.0)
            ))
        }
    }
}
