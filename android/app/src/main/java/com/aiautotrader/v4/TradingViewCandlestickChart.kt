package com.aiautotrader.v4

import android.content.Context
import android.widget.FrameLayout
import com.tradingview.lightweightcharts.api.chart.models.color.surface.SolidColor
import com.tradingview.lightweightcharts.api.chart.models.color.toIntColor
import com.tradingview.lightweightcharts.api.interfaces.SeriesApi
import com.tradingview.lightweightcharts.api.options.models.CandlestickSeriesOptions
import com.tradingview.lightweightcharts.api.options.models.gridLineOptions
import com.tradingview.lightweightcharts.api.options.models.gridOptions
import com.tradingview.lightweightcharts.api.options.models.layoutOptions
import com.tradingview.lightweightcharts.api.options.models.priceScaleOptions
import com.tradingview.lightweightcharts.api.options.models.timeScaleOptions
import com.tradingview.lightweightcharts.api.series.models.CandlestickData
import com.tradingview.lightweightcharts.api.series.models.Time
import com.tradingview.lightweightcharts.view.ChartsView
import java.time.OffsetDateTime

/**
 * Validated OHLCV input for the TradingView renderer.
 * The source API must already have rejected stale, open, gapped or malformed candles.
 */
internal data class TradingViewOhlc(
    val openTime: String,
    val open: Double,
    val high: Double,
    val low: Double,
    val close: Double,
    val volume: Double,
)

internal fun toTradingViewCandlestickData(rows: List<TradingViewOhlc>): List<CandlestickData> {
    var previousTimestamp = Long.MIN_VALUE
    return rows.map { row ->
        val timestamp = try {
            OffsetDateTime.parse(row.openTime).toInstant().epochSecond
        } catch (error: Exception) {
            throw IllegalArgumentException("candle open time must be ISO-8601 with an explicit offset", error)
        }
        val values = listOf(row.open, row.high, row.low, row.close, row.volume)
        require(values.all { it.isFinite() }) { "candle OHLCV values must be finite" }
        require(row.open > 0.0 && row.high > 0.0 && row.low > 0.0 && row.close > 0.0) {
            "candle prices must be positive"
        }
        require(row.volume >= 0.0) { "candle volume must not be negative" }
        require(row.high >= maxOf(row.open, row.close, row.low)) { "candle high is inconsistent" }
        require(row.low <= minOf(row.open, row.close, row.high)) { "candle low is inconsistent" }
        require(timestamp >= 0L && timestamp > previousTimestamp) { "candle timestamps must be non-negative and strictly increasing" }
        previousTimestamp = timestamp
        val floatValues = listOf(row.open, row.high, row.low, row.close).map { it.toFloat() }
        require(floatValues.all { it.isFinite() && it > 0f }) { "candle prices exceed chart precision" }
        CandlestickData(
            time = Time.Utc(timestamp),
            open = floatValues[0],
            high = floatValues[1],
            low = floatValues[2],
            close = floatValues[3],
        )
    }
}

/** TradingView Lightweight Charts renderer; it has no order or execution interface. */
internal class TradingViewCandlestickChart(context: Context) : FrameLayout(context) {
    private val chartView = ChartsView(context)
    private var candleSeries: SeriesApi? = null
    private var candleData: List<CandlestickData> = emptyList()
    private var isReady = false
    private var lightTheme = false

    init {
        addView(chartView, LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.MATCH_PARENT))
        chartView.setOnErrorListener {
            // Keep chart errors local to the visual component; they never affect paper state.
        }
        chartView.subscribeOnChartStateChange { state ->
            if (state is ChartsView.State.Ready && !isReady) {
                isReady = true
                configureChart()
                createCandlestickSeries()
            }
        }
    }

    fun setCandles(rows: List<TradingViewOhlc>) {
        candleData = try {
            toTradingViewCandlestickData(rows)
        } catch (_: IllegalArgumentException) {
            emptyList()
        }
        candleSeries?.setData(candleData)
    }

    fun setLightTheme(enabled: Boolean) {
        if (lightTheme == enabled) return
        lightTheme = enabled
        if (isReady) configureChart()
    }

    private fun configureChart() {
        val background = if (lightTheme) "#FFFFFF" else "#151D29"
        val foreground = if (lightTheme) "#202630" else "#F4F7FB"
        val grid = if (lightTheme) "#E5E0D7" else "#2A3544"
        chartView.api.applyOptions {
            layout = layoutOptions {
                this.background = SolidColor(background.toIntColor())
                textColor = foreground.toIntColor()
                attributionLogo = true
            }
            grid = gridOptions {
                vertLines = gridLineOptions { color = grid.toIntColor() }
                horzLines = gridLineOptions { color = grid.toIntColor() }
            }
            rightPriceScale = priceScaleOptions { borderColor = grid.toIntColor() }
            timeScale = timeScaleOptions {
                timeVisible = true
                secondsVisible = false
            }
        }
    }

    private fun createCandlestickSeries() {
        chartView.api.addCandlestickSeries(
            options = CandlestickSeriesOptions(
                upColor = "#43D9A3".toIntColor(),
                downColor = "#FF6B78".toIntColor(),
                wickUpColor = "#43D9A3".toIntColor(),
                wickDownColor = "#FF6B78".toIntColor(),
                borderVisible = false,
            ),
            onSeriesCreated = { series ->
                candleSeries = series
                series.setData(candleData)
            },
        )
    }
}
