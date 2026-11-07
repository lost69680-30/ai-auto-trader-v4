package com.aiautotrader.v4

import com.tradingview.lightweightcharts.api.series.models.CandlestickData
import com.tradingview.lightweightcharts.api.series.models.Time
import java.time.OffsetDateTime
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class TradingViewChartDataTest {
    private fun candle(
        openTime: String,
        open: Double = 100.0,
        high: Double = 105.0,
        low: Double = 98.0,
        close: Double = 103.0,
        volume: Double = 12.0,
    ) = MarketCandle(openTime, open, high, low, close, volume)

    @Test
    fun mapsValidatedIsoCandlesToUtcSecondsAndPreservesOhlc() {
        val source = candle("2026-11-07T12:00:00Z")

        val result: List<CandlestickData> = marketCandlesToTradingViewData(listOf(source))

        assertEquals(1, result.size)
        assertEquals(
            OffsetDateTime.parse(source.openTime).toEpochSecond(),
            (result.single().time as Time.Utc).timestamp,
        )
        assertEquals(100.0f, result.single().open)
        assertEquals(105.0f, result.single().high)
        assertEquals(98.0f, result.single().low)
        assertEquals(103.0f, result.single().close)
    }

    @Test
    fun rejectsNaiveTimestampsAndInvalidOrNonFiniteOhlcWithoutSynthesizingBars() {
        val source = listOf(
            candle("2026-11-07T12:00:00Z"),
            candle("2026-11-07T12:01:00"),
            candle("2026-11-07T12:02:00Z", open = 110.0, high = 105.0),
            candle("2026-11-07T12:03:00Z", close = Double.NaN),
        )

        val result = marketCandlesToTradingViewData(source)

        assertEquals(1, result.size)
        assertEquals(OffsetDateTime.parse(source.first().openTime).toEpochSecond(),
            (result.single().time as Time.Utc).timestamp)
    }

    @Test
    fun sortsCandlesChronologicallyAndDropsDuplicateOpenTimes() {
        val later = candle("2026-11-07T12:02:00Z", close = 104.0)
        val earlier = candle("2026-11-07T12:00:00Z", close = 101.0)
        val duplicateEarlier = candle("2026-11-07T12:00:00Z", close = 102.0)

        val result = marketCandlesToTradingViewData(listOf(later, earlier, duplicateEarlier))

        assertEquals(2, result.size)
        assertTrue((result[0].time as Time.Utc).timestamp < (result[1].time as Time.Utc).timestamp)
        assertEquals(101.0f, result[0].close)
    }
}
