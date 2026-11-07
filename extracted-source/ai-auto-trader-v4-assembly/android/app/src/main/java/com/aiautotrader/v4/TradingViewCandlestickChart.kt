package com.aiautotrader.v4

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.weight
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import com.tradingview.lightweightcharts.api.chart.models.color.surface.SolidColor
import com.tradingview.lightweightcharts.api.chart.models.color.toIntColor
import com.tradingview.lightweightcharts.api.interfaces.SeriesApi
import com.tradingview.lightweightcharts.api.options.models.CandlestickSeriesOptions
import com.tradingview.lightweightcharts.api.options.models.gridLineOptions
import com.tradingview.lightweightcharts.api.options.models.gridOptions
import com.tradingview.lightweightcharts.api.options.models.layoutOptions
import com.tradingview.lightweightcharts.api.options.models.priceScaleOptions
import com.tradingview.lightweightcharts.api.options.models.timeScaleOptions
import com.tradingview.lightweightcharts.view.ChartsView

@Composable
internal fun TradingViewCandlestickChart(
    modifier: Modifier,
    candles: List<MarketCandle>,
    backgroundColor: Color,
    textColor: Color,
    gridColor: Color,
    upColor: Color,
    downColor: Color,
    fallbackContent: @Composable (Modifier) -> Unit,
) {
    val context = LocalContext.current
    val uriHandler = LocalUriHandler.current
    val latestData = rememberUpdatedState(marketCandlesToTradingViewData(candles))
    val latestBackgroundColor = rememberUpdatedState(backgroundColor)
    val latestTextColor = rememberUpdatedState(textColor)
    val latestGridColor = rememberUpdatedState(gridColor)
    val latestUpColor = rememberUpdatedState(upColor)
    val latestDownColor = rememberUpdatedState(downColor)
    val seriesState = remember { mutableStateOf<SeriesApi?>(null) }
    val chartError = remember { mutableStateOf<String?>(null) }
    val appliedData = remember { mutableStateOf(emptyList<com.tradingview.lightweightcharts.api.series.models.CandlestickData>()) }
    val chartView = remember(context) { ChartsView(context) }

    val stateListener = remember(chartView) {
        { state: ChartsView.State ->
            when (state) {
                is ChartsView.State.Preparing -> {
                    seriesState.value = null
                    appliedData.value = emptyList()
                }
                is ChartsView.State.Ready -> {
                    chartError.value = null
                    if (seriesState.value == null) {
                        chartView.api.applyOptions {
                            layout = layoutOptions {
                                background = SolidColor(latestBackgroundColor.value.toArgb().toIntColor())
                                textColor = latestTextColor.value.toArgb().toIntColor()
                                attributionLogo = true
                            }
                            timeScale = timeScaleOptions {
                                timeVisible = true
                                secondsVisible = false
                                borderColor = latestGridColor.value.toArgb().toIntColor()
                                fixLeftEdge = true
                            }
                            rightPriceScale = priceScaleOptions {
                                borderColor = gridColor.toArgb().toIntColor()
                            }
                            grid = gridOptions {
                                horzLines = gridLineOptions {
                                    color = latestGridColor.value.toArgb().toIntColor()
                                }
                                vertLines = gridLineOptions {
                                    color = gridColor.toArgb().toIntColor()
                                }
                            }
                        }
                        chartView.api.addCandlestickSeries(
                            options = CandlestickSeriesOptions(
                                upColor = latestUpColor.value.toArgb().toIntColor(),
                                downColor = latestDownColor.value.toArgb().toIntColor(),
                                wickUpColor = latestUpColor.value.toArgb().toIntColor(),
                                wickDownColor = latestDownColor.value.toArgb().toIntColor(),
                                borderVisible = false,
                                lastValueVisible = true,
                                priceLineVisible = false,
                            ),
                        ) { createdSeries ->
                            seriesState.value = createdSeries
                            createdSeries.setData(latestData.value)
                            appliedData.value = latestData.value
                        }
                    }
                }
                is ChartsView.State.Error -> {
                    chartError.value = state.exception.localizedMessage ?: "TradingView chart is unavailable"
                }
            }
        }
    }

    DisposableEffect(chartView) {
        chartView.setOnErrorListener { error ->
            chartError.value = error.message ?: "TradingView chart reported an error"
        }
        chartView.subscribeOnChartStateChange(stateListener)
        onDispose {
            chartView.unsubscribeOnChartStateChange(stateListener)
            chartView.setOnErrorListener(null)
        }
    }

    Column(
        modifier = modifier
            .background(backgroundColor, RoundedCornerShape(10.dp))
            .padding(horizontal = 6.dp, vertical = 4.dp),
        verticalArrangement = Arrangement.spacedBy(2.dp),
    ) {
        Box(Modifier.fillMaxWidth().weight(1f)) {
            when {
                latestData.value.isEmpty() -> {
                    Text(
                        "No validated candle data available",
                        modifier = Modifier.align(Alignment.Center),
                        color = textColor,
                        fontSize = 10.sp,
                    )
                }
                chartError.value != null -> {
                    Box(Modifier.fillMaxSize()) {
                        fallbackContent(Modifier.fillMaxSize())
                        Text(
                            "TradingView unavailable · basic chart fallback",
                            modifier = Modifier
                                .align(Alignment.TopStart)
                                .background(backgroundColor.copy(alpha = 0.86f), RoundedCornerShape(5.dp))
                                .padding(horizontal = 6.dp, vertical = 3.dp),
                            color = textColor,
                            fontSize = 8.sp,
                        )
                    }
                }
                else -> {
                    AndroidView(
                        factory = { chartView },
                        modifier = Modifier.fillMaxSize(),
                        update = {
                            val currentData = latestData.value
                            val currentSeries = seriesState.value
                            if (currentSeries != null && currentData != appliedData.value) {
                                currentSeries.setData(currentData)
                                appliedData.value = currentData
                            }
                        },
                    )
                }
            }
        }
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.End,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                "TradingView Lightweight Charts™ · Copyright © 2025 TradingView, Inc.",
                modifier = Modifier.clickable {
                    uriHandler.openUri("https://www.tradingview.com/")
                },
                color = textColor,
                fontSize = 8.sp,
                fontWeight = FontWeight.Medium,
            )
        }
    }
}
