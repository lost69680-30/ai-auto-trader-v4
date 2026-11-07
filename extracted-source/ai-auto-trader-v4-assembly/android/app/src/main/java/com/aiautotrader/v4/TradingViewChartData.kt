package com.aiautotrader.v4

import com.tradingview.lightweightcharts.api.series.models.CandlestickData
import com.tradingview.lightweightcharts.api.series.models.Time
import java.time.OffsetDateTime

/**
 * Converts only validated backend candles to TradingView's UTC-second model.
 * Invalid rows are omitted; this function never creates or fills synthetic bars.
 */
internal fun marketCandlesToTradingViewData(
    candles: List<MarketCandle>,
): List<CandlestickData> {
    val ordered = sortedMapOf<Long, CandlestickData>()

    candles.forEach { candle ->
        val timestamp = runCatching {
            OffsetDateTime.parse(candle.openTime).toEpochSecond()
        }.getOrNull() ?: return@forEach

        val values = listOf(candle.open, candle.high, candle.low, candle.close)
        if (values.any { !it.isFinite() || it <= 0.0 }) return@forEach
        if (candle.high < maxOf(candle.open, candle.close)) return@forEach
        if (candle.low > minOf(candle.open, candle.close)) return@forEach
        if (candle.high < candle.low) return@forEach

        val open = candle.open.toFloat()
        val high = candle.high.toFloat()
        val low = candle.low.toFloat()
        val close = candle.close.toFloat()
        if (listOf(open, high, low, close).any { !it.isFinite() || it <= 0f }) {
            return@forEach
        }

        // Keep the first row for a timestamp and preserve strict chronological ordering.
        if (timestamp !in ordered) {
            ordered[timestamp] = CandlestickData(
                time = Time.Utc(timestamp),
                open = open,
                high = high,
                low = low,
                close = close,
            )
        }
    }

    return ordered.values.toList()
}
