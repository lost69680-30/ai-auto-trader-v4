package com.aiautotrader.v4

import android.os.Bundle
import java.net.HttpURLConnection
import java.net.URL
import org.json.JSONObject
import org.json.JSONArray
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.delay
import java.util.Locale
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

// UI-only theme preference. This changes presentation only; it cannot affect trading or risk controls.
private var lightTheme by mutableStateOf(false)
private val Background: Color get() = if (lightTheme) Color(0xFFF7F5F0) else Color(0xFF0B1018)
private val Panel: Color get() = if (lightTheme) Color(0xFFFFFFFF) else Color(0xFF151D29)
private val PanelAlt: Color get() = if (lightTheme) Color(0xFFEEEAE1) else Color(0xFF1B2635)
private val Gold = Color(0xFFD8B878)
private val Green: Color get() = if (lightTheme) Color(0xFF087F5B) else Color(0xFF43D9A3)
private val Red: Color get() = if (lightTheme) Color(0xFFC6283D) else Color(0xFFFF6B78)
private val Muted: Color get() = if (lightTheme) Color(0xFF5D6672) else Color(0xFF9BA9BA)
private val White: Color get() = if (lightTheme) Color(0xFF202630) else Color(0xFFF4F7FB)
private val Grid: Color get() = if (lightTheme) Color(0xFFD4CEC2) else Color(0xFF2A3544)


private data class MarketCandle(
    val openTime: String,
    val open: Double,
    val high: Double,
    val low: Double,
    val close: Double,
    val volume: Double,
)

private data class MarketInstrument(
    val symbol: String,
    val markPrice: Double,
    val markObservedAt: String,
    val candles: List<MarketCandle>,
)

private data class MarketFetchResult(
    val httpCode: Int,
    val backendConnected: Boolean,
    val backendStatus: String,
    val feedStatus: String,
    val items: List<MarketInstrument>,
    val reason: String,
    val safeResponse: Boolean,
)

// App-level observable state deliberately lives above individual navigation screens.
// Removing a composable from the screen tree must not erase an authenticated session.
private object BackendSession {
    val token = mutableStateOf("")
    val status = mutableStateOf("NOT CONNECTED")
    val details = mutableStateOf("Connect to the authenticated HTTPS backend.")
    val openPositions = mutableStateOf("—")
    val emergencyStop = mutableStateOf("UNKNOWN")
    val feedStatus = mutableStateOf("UNKNOWN")
    val marketItems = mutableStateOf<List<MarketInstrument>>(emptyList())
    val marketLoading = mutableStateOf(false)
    val marketMessage = mutableStateOf("Market data has not been loaded yet.")
    // Keep the last validated closed-candle snapshot when screens change or refreshes fail.
    val candleHistoryBySymbol = mutableStateOf<Map<String, Map<String, List<MarketCandle>>>>(emptyMap())
}

private fun normalizeSymbol(value: String): String = value.uppercase(Locale.ROOT).filter { it.isLetterOrDigit() }

private fun displaySymbol(value: String): String {
    val compact = normalizeSymbol(value)
    return when {
        compact.endsWith("USDT") && compact.length > 4 -> compact.dropLast(4) + "/USDT"
        compact.endsWith("USD") && compact.length > 3 -> compact.dropLast(3) + "/USD"
        else -> value
    }
}

private fun parseMarketItems(array: JSONArray): List<MarketInstrument> {
    val result = mutableListOf<MarketInstrument>()
    for (i in 0 until array.length()) {
        val row = array.optJSONObject(i) ?: continue
        if (row.optString("status") != "HEALTHY" || row.optBoolean("orders_authorized", true)) continue
        val symbol = row.optString("symbol").trim()
        val price = row.optDouble("mark_price", Double.NaN)
        if (symbol.isBlank() || !price.isFinite() || price <= 0.0) continue
        val candlesJson = row.optJSONArray("candles") ?: JSONArray()
        val candles = mutableListOf<MarketCandle>()
        for (j in 0 until candlesJson.length()) {
            val c = candlesJson.optJSONObject(j) ?: continue
            val open = c.optDouble("open", Double.NaN)
            val high = c.optDouble("high", Double.NaN)
            val low = c.optDouble("low", Double.NaN)
            val close = c.optDouble("close", Double.NaN)
            val volume = c.optDouble("volume", Double.NaN)
            val valid = open.isFinite() && high.isFinite() && low.isFinite() && close.isFinite() &&
                volume.isFinite() && open > 0.0 && close > 0.0 && low > 0.0 &&
                high >= maxOf(open, close, low) && low <= minOf(open, close) && volume >= 0.0
            if (c.optBoolean("is_closed", false) && valid) {
                candles += MarketCandle(c.optString("open_time"), open, high, low, close, volume)
            }
        }
        if (candles.isNotEmpty()) {
            result += MarketInstrument(symbol, price, row.optString("mark_observed_at"), candles.takeLast(100))
        }
    }
    return result
}

private data class TimeframeSnapshot(
    val timeframe: String,
    val candles: List<MarketCandle>,
    val analysis: TechnicalSnapshot?,
    val status: String,
    val bias: String,
)

// 1Y bars are derived in the backend only from twelve complete, closed monthly bars.
private val supportedTimeframes = listOf("1m", "5m", "15m", "1h", "4h", "1D", "1W", "1M", "1Y")
private fun timeframeBias(analysis: TechnicalSnapshot?): String {
    if (analysis == null || !analysis.enoughData) return "INSUFFICIENT DATA"
    val up = analysis.trend.startsWith("Uptrend") && analysis.momentum.startsWith("Positive")
    val down = analysis.trend.startsWith("Downtrend") && analysis.momentum.startsWith("Negative")
    return when { up -> "BULLISH"; down -> "BEARISH"; else -> "NEUTRAL / MIXED" }
}

private fun buildTimeframeSnapshots(candlesByTimeframe: Map<String, List<MarketCandle>>): List<TimeframeSnapshot> =
    supportedTimeframes.map { timeframe ->
        val candles = candlesByTimeframe[timeframe].orEmpty().takeLast(200)
        val analysis = if (candles.size >= 20) analyzeCandles(candles) else null
        val status = when {
            candles.isEmpty() -> "Unavailable · backend returned no closed candles for this interval"
            candles.size < 20 -> "Insufficient history · ${candles.size} complete candles; 20 required"
            candles.size < 50 -> "Limited history · ${candles.size} complete candles"
            else -> "Ready · ${candles.size} complete candles"
        }
        TimeframeSnapshot(timeframe, candles, analysis, status, timeframeBias(analysis))
    }

private data class TechnicalSnapshot(
    val ema20: String,
    val ema50: String,
    val rsi14: String,
    val macd: String,
    val bollinger: String,
    val atr14: String,
    val vwap: String,
    val supportResistance: String,
    val trend: String,
    val momentum: String,
    val volatility: String,
    val volume: String,
    val enoughData: Boolean,
)

private fun fmtIndicator(value: Double, digits: Int = 2): String =
    if (!value.isFinite()) "Unavailable" else String.format(Locale.US, "% .${digits}f", value).trim()

private fun ema(values: List<Double>, period: Int): Double? {
    if (values.size < period || period <= 0) return null
    val alpha = 2.0 / (period + 1.0)
    var current = values.take(period).average()
    for (value in values.drop(period)) current = alpha * value + (1.0 - alpha) * current
    return current.takeIf { it.isFinite() }
}

// Read-only indicator calculations from validated, closed backend candles. These never authorize orders.
private fun analyzeCandles(candles: List<MarketCandle>): TechnicalSnapshot {
    val c = candles.filter { it.open.isFinite() && it.high.isFinite() && it.low.isFinite() && it.close.isFinite() && it.volume.isFinite() && it.close > 0 && it.low > 0 && it.high >= it.low && it.volume >= 0 }
    val closes = c.map { it.close }
    val enough = c.size >= 50
    if (c.size < 20) return TechnicalSnapshot("Need 20+ candles", "Need 50+ candles", "Need 15+ candles", "Need 35+ candles", "Need 20+ candles", "Need 15+ candles", "Need positive volume", "Need 20+ candles", "Unavailable", "Unavailable", "Unavailable", "Insufficient data", false)
    val e20 = ema(closes, 20)
    val e50 = ema(closes, 50)
    val rsi = if (closes.size >= 15) {
        val changes = closes.zipWithNext { a, b -> b - a }
        var gains = changes.take(14).map { maxOf(it, 0.0) }.average()
        var losses = changes.take(14).map { maxOf(-it, 0.0) }.average()
        for ( change in changes.drop(14)) {
            gains = (gains * 13.0 + maxOf(change, 0.0)) / 14.0
            losses = (losses * 13.0 + maxOf(-change, 0.0)) / 14.0
        }
        when { losses == 0.0 && gains > 0.0 -> 100.0; losses == 0.0 -> 50.0; else -> 100.0 - 100.0 / (1.0 + gains / losses) }
    } else null
    val macdLine = if (closes.size >= 26) (ema(closes, 12) ?: Double.NaN) - (ema(closes, 26) ?: Double.NaN) else Double.NaN
    val macd = if (closes.size >= 35) {
        val lines = (26 until closes.size).mapNotNull { idx ->
            val sub = closes.take(idx + 1)
            val a = ema(sub, 12); val b = ema(sub, 26)
            if (a != null && b != null) a - b else null
        }
        val signal = ema(lines, 9)
        if (signal != null && macdLine.isFinite()) "${fmtIndicator(macdLine)} / signal ${fmtIndicator(signal)}" else "Unavailable"
    } else "Need 35+ candles"
    val last20 = closes.takeLast(20)
    val mean20 = last20.average()
    val std20 = kotlin.math.sqrt(last20.map { (it - mean20) * (it - mean20) }.average())
    val bb = "${fmtIndicator(mean20 - 2 * std20)} – ${fmtIndicator(mean20 + 2 * std20)}"
    val trueRanges = c.zipWithNext().mapIndexed { idx, pair ->
        val current = c[idx + 1]
        maxOf(current.high - current.low, kotlin.math.abs(current.high - pair.first.close), kotlin.math.abs(current.low - pair.first.close))
    }
    val atr = if (trueRanges.size >= 14) trueRanges.take(14).average().let { initial -> trueRanges.drop(14).fold(initial) { acc, v -> (acc * 13 + v) / 14.0 } } else null
    val totalVolume = c.sumOf { it.volume }
    val vwap = if (totalVolume > 0) c.sumOf { ((it.high + it.low + it.close) / 3.0) * it.volume } / totalVolume else null
    val recent = c.takeLast(20)
    val support = recent.minOfOrNull { it.low }
    val resistance = recent.maxOfOrNull { it.high }
    val avgVol = if (c.size >= 20) c.takeLast(20).map { it.volume }.average() else 0.0
    val volRatio = if (avgVol > 0) c.last().volume / avgVol else null
    val trend = when {
        e20 != null && e50 != null && closes.last() > e20 && e20 > e50 -> "Uptrend · price > EMA20 > EMA50"
        e20 != null && e50 != null && closes.last() < e20 && e20 < e50 -> "Downtrend · price < EMA20 < EMA50"
        e20 != null && closes.last() > e20 -> "Above EMA20 · mixed trend"
        e20 != null && closes.last() < e20 -> "Below EMA20 · mixed trend"
        else -> "Unavailable"
    }
    val momentum = when { rsi == null -> "Insufficient candles"; rsi >= 70 -> "RSI overbought (${fmtIndicator(rsi, 1)})"; rsi <= 30 -> "RSI oversold (${fmtIndicator(rsi, 1)})"; rsi > 55 -> "Positive (${fmtIndicator(rsi, 1)})"; rsi < 45 -> "Negative (${fmtIndicator(rsi, 1)})"; else -> "Neutral (${fmtIndicator(rsi, 1)})" }
    val volatility = if (atr != null && closes.last() > 0) "ATR ${fmtIndicator(atr)} (${fmtIndicator(100 * atr / closes.last(), 2)}% of price)" else "Need 15+ candles"
    return TechnicalSnapshot(
        e20?.let { fmtIndicator(it) } ?: "Need 20+ candles", e50?.let { fmtIndicator(it) } ?: "Need 50+ candles",
        rsi?.let { fmtIndicator(it, 1) } ?: "Need 15+ candles", macd, bb,
        atr?.let { fmtIndicator(it) } ?: "Need 15+ candles", vwap?.let { fmtIndicator(it) } ?: "Unavailable",
        if (support != null && resistance != null) "S ${fmtIndicator(support)} · R ${fmtIndicator(resistance)}" else "Unavailable",
        trend, momentum, volatility,
        if (volRatio != null) "Last / 20-candle avg: ${fmtIndicator(volRatio, 2)}×" else "Unavailable · zero/insufficient volume",
        enough
    )
}

private fun fetchMarketSnapshot(token: String): MarketFetchResult {
    val base = BuildConfig.PAPER_API_BASE_URL.trimEnd('/')
    if (!base.startsWith("https://")) {
        return MarketFetchResult(-1, false, "API NOT CONFIGURED", "UNKNOWN", emptyList(),
            "The HTTPS backend URL is not configured in this APK.", false)
    }
    fun get(path: String): Pair<Int, String> {
        val connection = (URL("$base$path").openConnection() as HttpURLConnection)
        try {
            connection.requestMethod = "GET"
            connection.connectTimeout = 8000
            connection.readTimeout = 8000
            connection.setRequestProperty("Authorization", "Bearer $token")
            connection.setRequestProperty("Accept", "application/json")
            connection.useCaches = false
            val code = connection.responseCode
            val stream = if (code in 200..299) connection.inputStream else connection.errorStream
            return code to (stream?.bufferedReader()?.use { it.readText() }.orEmpty())
        } finally {
            connection.disconnect()
        }
    }
    return try {
        val (statusCode, statusBody) = get("/api/v1/status")
        if (statusCode == 401) {
            MarketFetchResult(401, false, "UNAUTHORIZED", "UNAUTHORIZED", emptyList(),
                "Backend rejected the saved bearer token. Reconnect from Dashboard.", false)
        } else if (statusCode !in 200..299) {
            MarketFetchResult(statusCode, false, "OFFLINE / ERROR", "UNKNOWN", emptyList(),
                "Backend status check failed (HTTP $statusCode).", false)
        } else {
            val statusJson = JSONObject(statusBody)
            val safeStatus = statusJson.optString("mode") == "PAPER_ONLY" &&
                !statusJson.optBoolean("orders_authorized", true)
            if (!safeStatus) {
                MarketFetchResult(statusCode, false, "UNSAFE RESPONSE", "BLOCKED", emptyList(),
                    "Backend did not confirm PAPER_ONLY mode and disabled order authorization.", false)
            } else {
                val (marketsCode, marketsBody) = get("/api/v1/markets")
                val marketJson = if (marketsBody.isNotBlank()) JSONObject(marketsBody) else JSONObject()
                val feed = marketJson.optString("status", "UNKNOWN")
                val reason = marketJson.optString("reason", "No reason supplied by the market feed.")
                val safeMarkets = !marketJson.optBoolean("orders_authorized", true)
                val items = if (marketsCode in 200..299 && safeMarkets) {
                    parseMarketItems(marketJson.optJSONArray("items") ?: JSONArray())
                } else emptyList()
                val healthy = marketsCode in 200..299 && feed == "HEALTHY" && items.isNotEmpty() && safeMarkets
                val message = when {
                    healthy -> "Authenticated backend connected. ${items.size} validated instrument(s) loaded; order authorization remains disabled."
                    !safeMarkets -> "Market endpoint returned an unsafe or incomplete response. Data was rejected."
                    reason.isNotBlank() && reason != "null" -> reason
                    else -> "No validated market candles were returned (HTTP $marketsCode)."
                }
                MarketFetchResult(marketsCode, true, "CONNECTED · PAPER ONLY", feed, items, message, safeMarkets)
            }
        }
    } catch (_: Exception) {
        MarketFetchResult(-1, false, "OFFLINE / ERROR", "OFFLINE", emptyList(),
            "Market request failed. Check internet access and the backend deployment.", false)
    }
}

private fun fetchCandleHistory(token: String, symbol: String): Map<String, List<MarketCandle>> {
    val base = BuildConfig.PAPER_API_BASE_URL.trimEnd('/')
    if (!base.startsWith("https://") || token.length < 32 || normalizeSymbol(symbol).isBlank()) return emptyMap()
    val encodedSymbol = java.net.URLEncoder.encode(normalizeSymbol(symbol), "UTF-8")
    val connection = URL("$base/api/v1/markets/candles?symbol=$encodedSymbol").openConnection() as HttpURLConnection
    try {
        connection.requestMethod = "GET"
        connection.connectTimeout = 10000
        connection.readTimeout = 45000
        connection.setRequestProperty("Authorization", "Bearer $token")
        connection.setRequestProperty("Accept", "application/json")
        connection.useCaches = false
        val code = connection.responseCode
        val stream = if (code in 200..299) connection.inputStream else connection.errorStream
        val raw = stream?.bufferedReader()?.use { it.readText() }.orEmpty()
        if (code !in 200..299 || raw.isBlank()) return emptyMap()
        val root = JSONObject(raw)
        if (root.optString("mode") != "PAPER_ONLY" || root.optBoolean("orders_authorized", true) ||
            normalizeSymbol(root.optString("symbol")) != normalizeSymbol(symbol)) return emptyMap()
        val grouped = root.optJSONObject("candles_by_interval") ?: return emptyMap()
        val result = mutableMapOf<String, List<MarketCandle>>()
        for (timeframe in supportedTimeframes) {
            val apiInterval = when (timeframe) {
                "1D" -> "1d"
                "1W" -> "1w"
                "1M" -> "1M"
                "1Y" -> "1y"
                else -> timeframe
            }
            val rows = grouped.optJSONArray(apiInterval) ?: JSONArray()
            val candles = mutableListOf<MarketCandle>()
            for (i in 0 until rows.length()) {
                val c = rows.optJSONObject(i) ?: continue
                val open = c.optDouble("open", Double.NaN)
                val high = c.optDouble("high", Double.NaN)
                val low = c.optDouble("low", Double.NaN)
                val close = c.optDouble("close", Double.NaN)
                val volume = c.optDouble("volume", Double.NaN)
                val valid = open.isFinite() && high.isFinite() && low.isFinite() && close.isFinite() &&
                    volume.isFinite() && open > 0.0 && close > 0.0 && low > 0.0 &&
                    high >= maxOf(open, close, low) && low <= minOf(open, close) && volume >= 0.0
                if (c.optBoolean("is_closed", false) && valid) {
                    candles += MarketCandle(c.optString("open_time"), open, high, low, close, volume)
                }
            }
            result[timeframe] = candles.takeLast(200)
        }
        return result
    } finally {
        connection.disconnect()
    }
}

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        BackendSession.token.value = BackendTokenStore(applicationContext).read().orEmpty()
        setContent {
            MaterialTheme(colorScheme = if (lightTheme) lightColorScheme(primary = Gold, background = Background, surface = Panel,
                onBackground = White, onSurface = White) else darkColorScheme(primary = Gold, background = Background, surface = Panel,
                onBackground = White, onSurface = White)) {
                Surface(Modifier.fillMaxSize(), color = Background) { TraderApp() }
            }
        }
    }
}

@Composable
private fun TraderApp() {
    var screen by remember { mutableStateOf("Dashboard") }
    val screens = listOf("Dashboard", "AI Agents", "Markets", "Positions", "Risk Center")
    Column(Modifier.fillMaxSize().background(Background)) {
        Row(Modifier.fillMaxWidth().padding(horizontal = 20.dp, vertical = 16.dp), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
            Column {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(9.dp)) {
                    Image(
                        painter = painterResource(id = R.drawable.ic_solo_logo),
                        contentDescription = "Solo logo",
                        modifier = Modifier.size(34.dp)
                    )
                    Column {
                        Text("SOLO", color = Gold, fontSize = 11.sp, fontWeight = FontWeight.Bold, letterSpacing = 2.4.sp)
                        Text(screen, color = White, fontSize = 24.sp, fontWeight = FontWeight.Bold)
                    }
                }
            }
            Column(horizontalAlignment = Alignment.End, verticalArrangement = Arrangement.spacedBy(5.dp)) {
                Text("v4", color = Muted, fontWeight = FontWeight.SemiBold)
                Tag("PAPER ONLY", Green)
                Text(if (lightTheme) "☾ DARK" else "☼ LIGHT", color = Gold, fontSize = 10.sp,
                    fontWeight = FontWeight.Bold, modifier = Modifier.clickable { lightTheme = !lightTheme }.padding(vertical = 3.dp, horizontal = 5.dp))
            }
        }
        Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()).padding(horizontal = 16.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            screens.forEach { item ->
                val selected = screen == item || (item == "Markets" && screen == "Charts & Analysis")
                Box(Modifier.background(if (selected) Gold.copy(alpha = .16f) else Panel, RoundedCornerShape(12.dp))
                    .border(1.dp, if (selected) Gold.copy(alpha = .8f) else Grid, RoundedCornerShape(12.dp))
                    .clickable { screen = if (item == "Markets") "Charts & Analysis" else item }.padding(horizontal = 13.dp, vertical = 12.dp), contentAlignment = Alignment.Center) {
                    Text(item, color = if (selected) Gold else Muted, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
                }
            }
        }
        when (screen) {
            "Charts & Analysis", "Markets" -> AnalysisScreen()
            "AI Agents" -> AgentNetworkScreen()
            "Positions" -> PositionsScreen()
            "Risk Center" -> RiskCenterScreen()
            else -> DashboardScreen { screen = "Charts & Analysis" }
        }
    }
}

@Composable
private fun AgentNetworkScreen() {
    val agents = listOf(
        Triple("Research Agent", "Scans setups, support/resistance and market context", "READY · LOCAL UI"),
        Triple("Analysis Agent", "Reviews trend, momentum, volatility and indicators", "READY · LOCAL UI"),
        Triple("GAT Coordinator", "Designed to model relationships across instruments and agents", "NOT CONNECTED"),
        Triple("PPO / RL Policy", "Policy evaluation and learning from simulated outcomes", "NOT CONNECTED"),
        Triple("Risk Agent", "Checks sizing, exposure and trade constraints", "BACKEND REQUIRED"),
        Triple("Paper Simulator", "Simulated fills and position lifecycle", "BACKEND REQUIRED")
    )
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
        CardPanel {
            StatusLine("●", "ORCHESTRATOR · NOT CONNECTED", Color(0xFFFFC857))
            Text("Multi-agent coordination", fontSize = 20.sp, fontWeight = FontWeight.Bold)
            Text("This screen shows the intended agent topology. It does not claim agents are running or communicating until the backend is connected.", color = Muted, fontSize = 12.sp, lineHeight = 18.sp)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                MetricCard("Agents", "6", "Defined roles", Modifier.weight(1f))
                MetricCard("Message bus", "OFFLINE", "No live messages", Modifier.weight(1f))
            }
        }
        SectionTitle("INTENDED AGENT NETWORK")
        CardPanel {
            Text("Signal and context layer", color = Gold, fontSize = 11.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp)
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                AgentFlowNode("Research", "Market context", Modifier.weight(1f))
                Text("+", color = Muted, fontWeight = FontWeight.Bold)
                AgentFlowNode("Analysis", "Indicators", Modifier.weight(1f))
            }
            NetworkArrow("Signals are proposals only · not orders")
            AgentFlowNode("GAT Coordinator", "Relationship graph / agent context", Modifier.fillMaxWidth())
            NetworkArrow("Context passed to policy evaluation")
            AgentFlowNode("PPO / RL Policy", "Candidate action · offline / paper training", Modifier.fillMaxWidth())
            NetworkArrow("Every candidate must pass independent validation")
            AgentFlowNode("Risk Gate", "Final authority · fail closed", Modifier.fillMaxWidth(), warning = true)
            NetworkArrow("Only approved paper intents reach the simulator")
            AgentFlowNode("Paper Simulator / Ledger", "Simulated fills and reconciliation", Modifier.fillMaxWidth())
            Spacer(Modifier.height(4.dp))
            Text("Illustrative architecture only. Arrows describe the planned data flow; no agents or message bus are connected in this build.", color = Muted, fontSize = 11.sp, lineHeight = 16.sp)
        }
        SectionTitle("AGENT STATUS")
        agents.forEach { (name, description, status) ->
            CardPanel {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                    Text(name, fontSize = 14.sp, fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
                    Tag(status, if (status.startsWith("READY")) Green else Color(0xFFFFC857))
                }
                Text(description, color = Muted, fontSize = 12.sp, lineHeight = 17.sp)
            }
        }
        SectionTitle("COMMUNICATION CONTRACT")
        CardPanel {
            IndicatorRow("Message schema", "Versioned JSON · planned")
            IndicatorRow("Timeout / retries", "Fail closed · planned")
            IndicatorRow("Agent direct execution", "PROHIBITED")
            IndicatorRow("Risk gate final authority", "REQUIRED")
            IndicatorRow("Training mode", "Offline / paper only")
        }
    }
}

@Composable
private fun AgentFlowNode(title: String, subtitle: String, modifier: Modifier = Modifier, warning: Boolean = false) {
    Column(modifier.background(if (warning) Color(0xFFFFC857).copy(alpha = if (lightTheme) .12f else .08f) else PanelAlt, RoundedCornerShape(12.dp))
        .border(1.dp, if (warning) Color(0xFFFFC857).copy(alpha = .8f) else Grid, RoundedCornerShape(12.dp))
        .padding(horizontal = 12.dp, vertical = 11.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(title, color = if (warning) Color(0xFFFFC857) else White, fontSize = 13.sp, fontWeight = FontWeight.Bold)
        Text(subtitle, color = Muted, fontSize = 11.sp, lineHeight = 15.sp)
        Text(if (warning) "NOT CONNECTED · REQUIRED GATE" else "PLANNED · OFFLINE", color = if (warning) Color(0xFFFFC857) else Muted, fontSize = 9.sp, fontWeight = FontWeight.Bold, letterSpacing = .5.sp)
    }
}

@Composable
private fun NetworkArrow(label: String) {
    Column(Modifier.fillMaxWidth().padding(vertical = 7.dp), horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(3.dp)) {
        Text("↓", color = Gold, fontSize = 20.sp, fontWeight = FontWeight.Bold)
        Text(label, color = Muted, fontSize = 10.sp, lineHeight = 14.sp)
    }
}

@Composable
private fun PositionsScreen() {
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
        CardPanel {
            StatusLine("●", "PAPER LEDGER NOT CONNECTED", Color(0xFFFFC857))
            Text("Positions & orders", fontSize = 20.sp, fontWeight = FontWeight.Bold)
            Text("No positions or orders are displayed because verified ledger data is not available. Sample trades are intentionally not fabricated.", color = Muted, fontSize = 12.sp, lineHeight = 18.sp)
        }
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            MetricCard("Open positions", "—", "Awaiting ledger", Modifier.weight(1f))
            MetricCard("Pending orders", "—", "Paper service offline", Modifier.weight(1f))
        }
        SectionTitle("POSITION MONITOR")
        CardPanel {
            Text("No paper positions to display", fontWeight = FontWeight.SemiBold)
            Text("When the backend is connected, this area will show symbol, side, entry, current mark, unrealized P&L, stop-loss, take-profit and protection state.", color = Muted, fontSize = 12.sp, lineHeight = 18.sp)
        }
        SectionTitle("ORDER SAFETY")
        CardPanel {
            SafetyRow("Live exchange orders", "DISABLED", Green)
            SafetyRow("Paper fill reconciliation", "NOT CONNECTED", Color(0xFFFFC857))
            SafetyRow("Unknown position state", "BLOCK NEW ORDERS", Red)
        }
    }
}

@Composable
private fun RiskCenterScreen() {
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
        CardPanel {
            StatusLine("●", "FAIL-CLOSED MODE", Green)
            Text("Risk control center", fontSize = 20.sp, fontWeight = FontWeight.Bold)
            Text("The UI cannot approve trades. If risk validation, market data or position reconciliation is unavailable, the intended behavior is to reject new paper intents.", color = Muted, fontSize = 12.sp, lineHeight = 18.sp)
        }
        SectionTitle("INDEPENDENT SAFEGUARDS")
        CardPanel {
            SafetyRow("Risk gate", "BACKEND REQUIRED", Color(0xFFFFC857))
            SafetyRow("Max risk / exposure limits", "NOT LOADED", Color(0xFFFFC857))
            SafetyRow("Stale-data rejection", "REQUIRED", Green)
            SafetyRow("Unknown account state", "BLOCK NEW ORDERS", Red)
            SafetyRow("Agent direct exchange access", "PROHIBITED", Green)
            SafetyRow("Live execution", "DISABLED", Green)
        }
        SectionTitle("EMERGENCY STOP")
        CardPanel {
            Text("Emergency stop requires an authenticated backend connection", fontWeight = FontWeight.SemiBold)
            Text("Use the authenticated backend connection card on the dashboard to activate the persistent paper stop. This is not a live-exchange kill switch; live orders remain disabled.", color = Muted, fontSize = 12.sp, lineHeight = 18.sp)
            Button(onClick = {}, enabled = false, modifier = Modifier.fillMaxWidth(), colors = ButtonDefaults.buttonColors(disabledContainerColor = PanelAlt, disabledContentColor = Muted)) { Text("STOP CONTROL UNAVAILABLE") }
        }
        SectionTitle("RISK EVENT LOG")
        CardPanel { Text("No risk events received", color = Muted, fontSize = 12.sp) }
    }
}

@Composable
private fun ReadOnlyBackendConnectionCard() {
    val context = androidx.compose.ui.platform.LocalContext.current
    val tokenStore = remember { BackendTokenStore(context) }
    var token by BackendSession.token
    var status by BackendSession.status
    var details by BackendSession.details
    var loading by remember { mutableStateOf(false) }
    var openPositions by BackendSession.openPositions
    var emergencyStop by BackendSession.emergencyStop
    var feedStatus by BackendSession.feedStatus
    val scope = rememberCoroutineScope()

    CardPanel {
        Text("READ-ONLY BACKEND CONNECTION", color = Gold, fontSize = 11.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp)
        Text("API: ${if (BuildConfig.PAPER_API_BASE_URL.isBlank()) "not configured" else BuildConfig.PAPER_API_BASE_URL}", color = Muted, fontSize = 11.sp)
        OutlinedTextField(value = token, onValueChange = { token = it }, label = { Text("Bearer API token") },
            singleLine = true, modifier = Modifier.fillMaxWidth(), visualTransformation = androidx.compose.ui.text.input.PasswordVisualTransformation())
        Button(enabled = !loading && token.length >= 32 && BuildConfig.PAPER_API_BASE_URL.startsWith("https://"),
            onClick = {
      val normalizedToken = token.trim()
      val tokenSaved = try {
          tokenStore.save(normalizedToken)
          true
      } catch (_: Exception) {
          false
      }
      if (!tokenSaved) {
          status = "TOKEN STORAGE ERROR"
          details = "No API request was sent because the token could not be stored securely."
          return@Button
      }
      token = normalizedToken
      loading = true
      status = "CHECKING"
      scope.launch {
                    val result = withContext(Dispatchers.IO) {
                        try {
                            val base = BuildConfig.PAPER_API_BASE_URL.trimEnd('/')
                            fun get(path: String): Pair<Int, String> {
                                val connection = (URL("$base$path").openConnection() as HttpURLConnection)
                                try {
                                    connection.requestMethod = "GET"
                                    connection.connectTimeout = 5000
                                    connection.readTimeout = 5000
                                    connection.setRequestProperty("Authorization", "Bearer $token")
                                    connection.setRequestProperty("Accept", "application/json")
                                    val code = connection.responseCode
                                    val stream = if (code in 200..299) connection.inputStream else connection.errorStream
                                    return code to (stream?.bufferedReader()?.use { it.readText() }.orEmpty())
                                } finally { connection.disconnect() }
                            }
                            val (code, body) = get("/api/v1/status")
                            if (code == 401) {
                                Triple("UNAUTHORIZED", "Token rejected. Check the API token and try again.", null)
                            } else if (code !in 200..299) {
                                Triple("API ERROR ($code)", "The server did not return a valid status response.", null)
                            } else {
                                val json = JSONObject(body)
                                if (json.optString("mode") != "PAPER_ONLY" || json.optBoolean("orders_authorized", true)) {
                                    Triple("UNSAFE RESPONSE", "Backend response did not confirm paper-only mode; data was not accepted.", null)
                                } else {
                                    val (positionsCode, positionsBody) = get("/api/v1/positions")
                                    val positionsLabel = if (positionsCode in 200..299) "AVAILABLE" else "UNAVAILABLE"
                                    // Parse the list response as JSON too; reject malformed responses rather than
                                    // displaying an apparently connected state from a partial HTTP check.
                                    if (positionsCode in 200..299) JSONObject(positionsBody).optJSONArray("items") ?: JSONArray()
                                    val feed = json.optJSONObject("feed")?.optString("status", "UNKNOWN") ?: "UNKNOWN"
                                    val stop = if (json.optBoolean("emergency_stop_active", false)) "ACTIVE" else "INACTIVE"
                                    val summary = "Mode: PAPER ONLY · Open positions: ${json.optInt("open_position_count", 0)} · Positions API: $positionsLabel"
                                    Triple("CONNECTED · PAPER ONLY", summary, Triple(positionsLabel, stop, feed))
                                }
                            }
                        } catch (_: Exception) {
                            Triple("OFFLINE / ERROR", "Connection failed. Verify the HTTPS URL, hosting status, and network access.", null)
                        }
                    }
                    status = result.first
                    details = result.second
                    result.third?.let { summary ->
                        openPositions = summary.first
                        emergencyStop = summary.second
                        feedStatus = summary.third
                    }
                    loading = false
                }
            }, modifier = Modifier.fillMaxWidth(), colors = ButtonDefaults.buttonColors(containerColor = Gold, contentColor = Background)) {
            Text(if (loading) "CHECKING…" else "CONNECT & LOAD STATUS")
        }
        Text(status, color = if (status.startsWith("CONNECTED")) Green else Color(0xFFFFC857), fontWeight = FontWeight.Bold, fontSize = 12.sp)
        Text(details, color = Muted, fontSize = 12.sp, lineHeight = 17.sp)
        if (status.startsWith("CONNECTED")) {
            Divider(color = Grid)
            IndicatorRow("Positions API", openPositions)
            IndicatorRow("Emergency stop", emergencyStop)
            IndicatorRow("Market feed", feedStatus)
            Button(
                enabled = !loading && emergencyStop != "ACTIVE",
                onClick = {
                    loading = true
                    scope.launch {
                        val result = withContext(Dispatchers.IO) {
                            try {
                                val base = BuildConfig.PAPER_API_BASE_URL.trimEnd('/')
                                val connection = (URL("$base/api/v1/emergency-stop").openConnection() as HttpURLConnection)
                                try {
                                    connection.requestMethod = "POST"
                                    connection.connectTimeout = 5000
                                    connection.readTimeout = 5000
                                    connection.doOutput = true
                                    connection.setRequestProperty("Authorization", "Bearer $token")
                                    connection.setRequestProperty("Content-Type", "application/json")
                                    connection.setRequestProperty("Accept", "application/json")
                                    connection.outputStream.use { it.write("{\"reason\":\"Emergency stop activated from Android app\"}".toByteArray(Charsets.UTF_8)) }
                                    val code = connection.responseCode
                                    val stream = if (code in 200..299) connection.inputStream else connection.errorStream
                                    val response = stream?.bufferedReader()?.use { it.readText() }.orEmpty()
                                    if (code in 200..299) {
                                        val json = JSONObject(response)
                                        if (json.optBoolean("emergency_stop_active", false) && !json.optBoolean("orders_authorized", true)) {
                                            "ACTIVE" to "Emergency stop activated. Paper orders remain disabled."
                                        } else "UNKNOWN" to "Server response did not confirm the stop; verify backend status."
                                    } else "UNKNOWN" to "Stop request failed (HTTP $code). Verify backend status."
                                } finally { connection.disconnect() }
                            } catch (_: Exception) {
                                "UNKNOWN" to "Could not confirm stop activation. Check connection and backend status."
                            }
                        }
                        emergencyStop = result.first
                        details = result.second
                        loading = false
                    }
                },
                modifier = Modifier.fillMaxWidth(),
                colors = ButtonDefaults.buttonColors(containerColor = Red, contentColor = Color.White)
            ) { Text(if (loading) "SENDING STOP…" else if (emergencyStop == "ACTIVE") "EMERGENCY STOP ACTIVE" else "ACTIVATE EMERGENCY STOP") }
        }
        Button(
            enabled = !loading && token.isNotBlank(),
            onClick = {
                tokenStore.clear()
                token = ""
                status = "NOT CONNECTED"
                details = "Saved backend token cleared from this device."
                openPositions = "—"
                emergencyStop = "UNKNOWN"
                feedStatus = "UNKNOWN"
                BackendSession.marketItems.value = emptyList()
                BackendSession.marketMessage.value = "Backend token cleared; no authenticated market request will be made."
            },
            modifier = Modifier.fillMaxWidth(),
            colors = ButtonDefaults.buttonColors(containerColor = PanelAlt, contentColor = White)
        ) { Text("FORGET SAVED TOKEN") }
        Text("The bearer token is encrypted on this device with Android Keystore. Clearing it here does not change the backend emergency-stop state. Do not use an exchange key here.", color = Muted, fontSize = 10.sp)
    }
}

@Composable
private fun DashboardScreen(openAnalysis: () -> Unit) {
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(20.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        ReadOnlyBackendConnectionCard()
        CardPanel {
            val connected = BackendSession.status.value.startsWith("CONNECTED")
            StatusLine("●", if (connected) "BACKEND CONNECTED · PAPER ONLY" else "BACKEND STATUS · ${BackendSession.status.value}", if (connected) Green else Color(0xFFFFC857))
            Spacer(Modifier.height(8.dp))
            Text("Paper mode only", fontSize = 20.sp, fontWeight = FontWeight.Bold)
            Text(if (connected) "Authenticated backend session retained across navigation. Market feed: ${BackendSession.feedStatus.value}. ${BackendSession.marketMessage.value}" else "Live balances, positions and market data remain unavailable until the authenticated API is connected.", color = Muted, fontSize = 13.sp, lineHeight = 19.sp)
        }
        SectionTitle("ACCOUNT OVERVIEW")
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            MetricCard("Paper equity", "—", "Waiting for backend", Modifier.weight(1f))
            MetricCard("Daily P&L", "—", "No account data", Modifier.weight(1f))
        }
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            MetricCard("Open positions", BackendSession.openPositions.value, "Positions API", Modifier.weight(1f))
            MetricCard("Market feed", BackendSession.feedStatus.value, BackendSession.marketMessage.value, Modifier.weight(1f))
        }
        CardPanel {
            Text("CHARTS & ANALYSIS", color = Gold, fontSize = 11.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.2.sp)
            Text("Candlesticks, indicators, performance and AI market commentary", color = Muted, fontSize = 13.sp, lineHeight = 19.sp)
            Button(onClick = openAnalysis, colors = ButtonDefaults.buttonColors(containerColor = Gold, contentColor = Background), modifier = Modifier.fillMaxWidth()) { Text("Open Charts & Analysis") }
        }
        SectionTitle("SAFETY CONTROLS")
        CardPanel {
            SafetyRow("Exchange order execution", "DISABLED", Green)
            SafetyRow("Risk gate", "BLOCKED · no order authorization", Red)
            SafetyRow("Emergency stop", BackendSession.emergencyStop.value, if (BackendSession.emergencyStop.value == "ACTIVE") Red else Color(0xFFFFC857))
        }
        Text("Build milestone 2 · Charts and analysis UI. No exchange credentials are stored in this app. AI analysis is advisory; independent risk validation has final authority.", color = Muted, fontSize = 11.sp, lineHeight = 17.sp)
    }
}

@Composable
private fun AnalysisScreen() {
    var symbol by remember { mutableStateOf("BTC/USDT") }
    var timeframe by remember { mutableStateOf("15m") }
    var selectedTab by remember { mutableStateOf("Market") }
    val timeframes = supportedTimeframes
    val sessionToken = BackendSession.token.value
    LaunchedEffect(sessionToken) {
        if (sessionToken.length < 32) {
            BackendSession.marketItems.value = emptyList()
            BackendSession.marketMessage.value = "Connect with the backend bearer token on Dashboard to load validated market data."
            BackendSession.feedStatus.value = "NOT AUTHENTICATED"
            BackendSession.marketLoading.value = false
        } else {
            while (true) {
                BackendSession.marketLoading.value = true
                val response = withContext(Dispatchers.IO) { fetchMarketSnapshot(sessionToken) }
                BackendSession.status.value = response.backendStatus
                BackendSession.feedStatus.value = response.feedStatus
                BackendSession.marketMessage.value = response.reason
                BackendSession.details.value = if (response.backendConnected) {
                    "${response.backendStatus} · Market feed: ${response.feedStatus} · ${response.reason}"
                } else {
                    response.reason
                }
                // Keep the last validated market snapshot during transient failures. Its feed
                // status above still reports the outage, and it never grants order authority.
                if (response.backendConnected && response.safeResponse &&
                    response.feedStatus == "HEALTHY" && response.items.isNotEmpty()) {
                    BackendSession.marketItems.value = response.items
                }
                BackendSession.marketLoading.value = false
                delay(30_000)
            }
        }
    }
    val loadedMarkets = BackendSession.marketItems.value
    val selectedMarket = loadedMarkets.firstOrNull { normalizeSymbol(it.symbol) == normalizeSymbol(symbol) }
    val candleSymbolKey = normalizeSymbol(selectedMarket?.symbol ?: symbol)
    val candleHistoryByTimeframe = BackendSession.candleHistoryBySymbol.value[candleSymbolKey].orEmpty()
    var candleHistoryLoading by remember { mutableStateOf(false) }
    LaunchedEffect(sessionToken, selectedMarket?.symbol, candleSymbolKey) {
        if (sessionToken.length >= 32 && selectedMarket != null && candleSymbolKey.isNotBlank()) {
            while (true) {
                candleHistoryLoading = true
                val history = withContext(Dispatchers.IO) {
                    try { fetchCandleHistory(sessionToken, selectedMarket.symbol) } catch (_: Exception) { emptyMap() }
                }
                // Update only intervals with usable candles so an empty/failed refresh cannot
                // erase a previously validated chart for the same symbol.
                val nonEmptyHistory = history.filterValues { it.isNotEmpty() }
                if (nonEmptyHistory.isNotEmpty()) {
                    val previous = BackendSession.candleHistoryBySymbol.value[candleSymbolKey].orEmpty()
                    BackendSession.candleHistoryBySymbol.value =
                        BackendSession.candleHistoryBySymbol.value +
                            (candleSymbolKey to (previous + nonEmptyHistory))
                }
                candleHistoryLoading = false
                delay(60_000)
            }
        } else {
            candleHistoryLoading = false
        }
    }
    val timeframeSnapshots = remember(selectedMarket?.symbol, candleHistoryByTimeframe, timeframe) {
        buildTimeframeSnapshots(candleHistoryByTimeframe)
    }
    val selectedTimeframe = timeframeSnapshots.firstOrNull { it.timeframe == timeframe }
    val displayedCandles = selectedTimeframe?.candles.orEmpty()
    val technical = selectedTimeframe?.analysis
    LaunchedEffect(loadedMarkets.map { it.symbol }) {
        if (loadedMarkets.isNotEmpty() && loadedMarkets.none { normalizeSymbol(it.symbol) == normalizeSymbol(symbol) }) {
            symbol = displaySymbol(loadedMarkets.first().symbol)
        }
    }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
        CardPanel {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                Column {
                    Text("MARKET CHART", color = Gold, fontSize = 11.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.3.sp)
                    Text(symbol, fontSize = 22.sp, fontWeight = FontWeight.Bold)
                    if (selectedMarket != null) {
                        Text("Mark price: ${formatMarketPrice(selectedMarket.markPrice)}", color = Green, fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
                    }
                }
                Tag(
                    if (selectedMarket != null && BackendSession.feedStatus.value == "HEALTHY") "VALIDATED FEED"
                    else if (selectedMarket != null) "LAST GOOD SNAPSHOT"
                    else if (BackendSession.marketLoading.value) "LOADING" else "NO LIVE DATA",
                    if (selectedMarket != null && BackendSession.feedStatus.value == "HEALTHY") Green else Color(0xFFFFC857)
                )
            }
            val visibleSymbols = if (loadedMarkets.isNotEmpty()) loadedMarkets.map { displaySymbol(it.symbol) }.distinct() else listOf("BTC/USDT", "ETH/USDT", "EUR/USD", "GBP/USD")
            Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(7.dp)) {
                visibleSymbols.forEach { s -> ChoiceChip(s, normalizeSymbol(symbol) == normalizeSymbol(s)) { symbol = s } }
            }
            Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                timeframes.forEach { t -> ChoiceChip(t, timeframe == t) { timeframe = t } }
            }
            Text("Feed: ${BackendSession.feedStatus.value} · ${BackendSession.marketMessage.value}", color = Muted, fontSize = 11.sp, lineHeight = 16.sp)
            Text("Selected interval: $timeframe · loaded directly from validated, closed backend candles. No missing candles are fabricated.", color = Muted, fontSize = 10.sp, lineHeight = 14.sp)
            CandlestickChart(Modifier.fillMaxWidth().height(190.dp), displayedCandles)
            if (selectedMarket == null) {
                Text(if (BackendSession.marketLoading.value || candleHistoryLoading) "Loading authenticated interval-specific candles…" else "No validated candles for $symbol. The chart will remain empty rather than fabricate prices.", color = Muted, fontSize = 12.sp, lineHeight = 17.sp)
            } else {
                Text("${displayedCandles.size} complete $timeframe candles · last observed ${selectedMarket.markObservedAt.ifBlank { "timestamp unavailable" }}", color = Muted, fontSize = 10.sp, lineHeight = 14.sp)
            }
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                LegendDot("Volume", Gold)
                LegendDot("Up candle", Green)
                LegendDot("Down candle", Red)
            }
        }
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            listOf("Market", "Performance").forEach { t ->
                Box(Modifier.weight(1f).background(if (selectedTab == t) Gold.copy(alpha = .15f) else Panel, RoundedCornerShape(10.dp))
                    .border(1.dp, if (selectedTab == t) Gold else Grid, RoundedCornerShape(10.dp)).clickable { selectedTab = t }.padding(11.dp), contentAlignment = Alignment.Center) {
                    Text(t, color = if (selectedTab == t) Gold else Muted, fontWeight = FontWeight.SemiBold, fontSize = 12.sp)
                }
            }
        }
        if (selectedTab == "Market") {
            SectionTitle("TECHNICAL ANALYSIS")
            CardPanel {
                IndicatorRow("EMA 20", technical?.ema20 ?: "Unavailable · no validated candles")
                IndicatorRow("EMA 50", technical?.ema50 ?: "Unavailable · no validated candles")
                IndicatorRow("RSI 14", technical?.rsi14 ?: "Unavailable · no validated candles")
                IndicatorRow("MACD 12/26/9", technical?.macd ?: "Unavailable · no validated candles")
                IndicatorRow("Bollinger Bands 20, ±2σ", technical?.bollinger ?: "Unavailable · no validated candles")
                IndicatorRow("ATR 14 · volatility", technical?.atr14 ?: "Unavailable · no validated candles")
                IndicatorRow("VWAP", technical?.vwap ?: "Unavailable · no validated candles")
                IndicatorRow("Support / resistance · 20 candles", technical?.supportResistance ?: "Unavailable · no validated candles")
                IndicatorRow("Volume analysis", technical?.volume ?: "Unavailable · no validated candles")
                IndicatorRow("Selected timeframe", "$timeframe · ${selectedTimeframe?.status ?: "Unavailable"}")
            }
            SectionTitle("MULTI-TIMEFRAME AGREEMENT")
            CardPanel {
                Text("Same symbol · independently calculated per interval", color = Muted, fontSize = 11.sp)
                timeframeSnapshots.forEach { snapshot ->
                    Divider(color = Grid)
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                        Column(Modifier.weight(1f)) {
                            Text(snapshot.timeframe, color = if (snapshot.timeframe == timeframe) Gold else White, fontWeight = FontWeight.Bold, fontSize = 13.sp)
                            Text(snapshot.status, color = Muted, fontSize = 10.sp, lineHeight = 14.sp)
                        }
                        Text(snapshot.bias, color = when (snapshot.bias) { "BULLISH" -> Green; "BEARISH" -> Red; else -> Gold }, fontSize = 10.sp, fontWeight = FontWeight.Bold)
                    }
                }
                val ready = timeframeSnapshots.filter { it.bias == "BULLISH" || it.bias == "BEARISH" }
                val bulls = ready.count { it.bias == "BULLISH" }
                val bears = ready.count { it.bias == "BEARISH" }
                val summary = when {
                    ready.isEmpty() -> "No agreement conclusion · insufficient timeframe history"
                    bulls > 0 && bears > 0 -> "CONFLICT · bullish and bearish timeframes disagree"
                    bulls > 0 -> "Bullish alignment in ${bulls} timeframe(s); other intervals may be neutral or unavailable"
                    else -> "Bearish alignment in ${bears} timeframe(s); other intervals may be neutral or unavailable"
                }
                Text(summary, color = Gold, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
            }
            SectionTitle("EVIDENCE & UNCERTAINTY")
            CardPanel {
                val usableIntervals = timeframeSnapshots.count { it.candles.size >= 50 }
                val bullish = timeframeSnapshots.count { it.bias == "BULLISH" }
                val bearish = timeframeSnapshots.count { it.bias == "BEARISH" }
                AnalysisValue(
                    "History coverage",
                    "\${usableIntervals} / \${timeframeSnapshots.size} intervals have 50+ closed candles",
                    Muted
                )
                AnalysisValue(
                    "Signal confidence",
                    "NOT CALIBRATED · no validated out-of-sample confidence model",
                    Gold
                )
                AnalysisValue(
                    "Evidence uncertainty",
                    when {
                        usableIntervals == 0 -> "HIGH · no interval has sufficient history"
                        bullish > 0 && bearish > 0 -> "HIGH · timeframes conflict"
                        usableIntervals < timeframeSnapshots.size -> "ELEVATED · some intervals lack history"
                        else -> "UNQUANTIFIED · indicator agreement is not a probability"
                    },
                    if (usableIntervals == 0 || (bullish > 0 && bearish > 0)) Red else Gold
                )
                AnalysisValue("Order authorization", "DISABLED · read-only market analysis", Red)
                Text(
                    "A bullish/bearish label describes the observed candle history only. It is not a win probability, a verified strategy signal, or permission to place an order.",
                    color = Muted, fontSize = 11.sp, lineHeight = 16.sp
                )
            }
            SectionTitle("AI MARKET ANALYSIS")
            CardPanel {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                    Text(symbol, fontWeight = FontWeight.Bold, fontSize = 16.sp)
                    Tag("ADVISORY ONLY", Gold)
                }
                AnalysisValue("Trend", technical?.trend ?: "Unavailable · no validated candles", Muted)
                AnalysisValue("Momentum", technical?.momentum ?: "Unavailable · no validated candles", Muted)
                AnalysisValue("Volatility", technical?.volatility ?: "Unavailable · no validated candles", Muted)
                AnalysisValue("Review readiness", if (technical?.enoughData == true) "Indicators calculated · review only" else "Insufficient candles for full EMA20/50 review", Gold)
                AnalysisValue("AI signal", "HOLD · no approved strategy signal", Gold)
                AnalysisValue("Risk check", "BLOCKED · authorization not verified in this check", Red)
                Text("Indicators are calculated locally from validated, closed backend candles and are advisory only. They do not constitute a validated strategy signal, do not authorize orders, and do not guarantee profitability. Missing, stale or insufficient data must never be used to infer a trade.", color = Muted, fontSize = 12.sp, lineHeight = 18.sp)
            }
        } else {
            SectionTitle("PERFORMANCE ANALYTICS")
            AnalyticsChartCard("Equity curve", "No paper ledger data connected", ChartKind.LINE)
            AnalyticsChartCard("P&L history", "No closed-trade history connected", ChartKind.BARS)
            AnalyticsChartCard("Drawdown", "Unavailable until equity history exists", ChartKind.LINE)
            AnalyticsChartCard("Win / loss distribution", "No completed trades", ChartKind.BARS)
            AnalyticsChartCard("Portfolio allocation", "No open paper positions", ChartKind.ALLOCATION)
            AnalyticsChartCard("Performance by strategy & symbol", "No strategy results connected", ChartKind.BARS)
        }
        Text("Performance status: unavailable until authenticated paper-ledger analytics are connected. No account returns or trading results are fabricated.", color = Muted, fontSize = 11.sp, lineHeight = 16.sp)
    }
}

private enum class ChartKind { LINE, BARS, ALLOCATION }

@Composable
private fun AnalyticsChartCard(title: String, note: String, kind: ChartKind) {
    CardPanel {
        Text(title, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
        Text(note, color = Muted, fontSize = 11.sp)
        EmptyChart(Modifier.fillMaxWidth().height(92.dp), kind)
    }
}

@Composable
private fun CandlestickChart(modifier: Modifier = Modifier, candles: List<MarketCandle>) {
    Canvas(modifier.background(Panel, RoundedCornerShape(10.dp)).padding(8.dp)) {
        val w = size.width
        val h = size.height
        val chartH = h * .78f
        for (i in 0..4) {
            val y = chartH * i / 4
            drawLine(Grid, Offset(0f, y), Offset(w, y), 1f)
        }
        val visible = candles.takeLast(60)
        if (visible.isEmpty() || w <= 0f || chartH <= 0f) return@Canvas
        val minPrice = visible.minOf { it.low }
        val maxPrice = visible.maxOf { it.high }
        val range = (maxPrice - minPrice).takeIf { it > 0.0 } ?: (maxPrice * 0.001).coerceAtLeast(0.00000001)
        fun priceY(value: Double): Float = (((maxPrice - value) / range) * chartH.toDouble()).toFloat().coerceIn(0f, chartH)
        val step = w / visible.size
        val maxVolume = visible.maxOf { it.volume }.coerceAtLeast(0.00000001)
        visible.forEachIndexed { i, candle ->
            val x = step * (i + .5f)
            val color = if (candle.close >= candle.open) Green else Red
            drawLine(color, Offset(x, priceY(candle.high)), Offset(x, priceY(candle.low)), 2f)
            val openY = priceY(candle.open)
            val closeY = priceY(candle.close)
            val top = minOf(openY, closeY)
            val bottom = maxOf(openY, closeY)
            drawRect(color, Offset(x - step * .27f, top), Size(step * .54f, maxOf(2f, bottom - top)))
            val volumeH = (candle.volume / maxVolume).toFloat() * (h - chartH) * .85f
            drawRect(color.copy(alpha = .38f), Offset(x - step * .22f, h - volumeH), Size(step * .44f, volumeH))
        }
    }
}

private fun formatMarketPrice(value: Double): String = when {
    value >= 1000.0 -> String.format(Locale.US, "%.2f", value)
    value >= 1.0 -> String.format(Locale.US, "%.4f", value)
    else -> String.format(Locale.US, "%.8f", value)
}

@Composable
private fun EmptyChart(modifier: Modifier, kind: ChartKind) {
    Canvas(modifier.background(PanelAlt, RoundedCornerShape(10.dp)).padding(8.dp)) {
        for (i in 0..3) drawLine(Grid, Offset(0f,size.height*i/3), Offset(size.width,size.height*i/3), 1f)
        if (kind == ChartKind.ALLOCATION) {
            drawCircle(Grid, radius = minOf(size.height*.35f, size.width*.12f), center = Offset(size.width/2,size.height/2), style = androidx.compose.ui.graphics.drawscope.Stroke(width=14f))
        } else if (kind == ChartKind.BARS) {
            val bw = size.width / 12f
            for (i in 0..7) {
                val bh = size.height * ((i*5%8)+1) / 10f
                drawRect(if (i%3==0) Red.copy(alpha=.6f) else Green.copy(alpha=.6f), Offset(bw*(i+2), size.height-bh), Size(bw*.55f,bh))
            }
        } else {
            val path = Path().apply {
                moveTo(0f,size.height*.72f); lineTo(size.width*.16f,size.height*.62f); lineTo(size.width*.31f,size.height*.68f)
                lineTo(size.width*.48f,size.height*.37f); lineTo(size.width*.64f,size.height*.48f); lineTo(size.width*.81f,size.height*.24f); lineTo(size.width,size.height*.30f)
            }
            drawPath(path, Muted.copy(alpha=.7f), style=androidx.compose.ui.graphics.drawscope.Stroke(width=2.5f))
        }
    }
}

@Composable
private fun CardPanel(content: @Composable ColumnScope.() -> Unit) {
    Card(colors = CardDefaults.cardColors(containerColor = Panel), shape = RoundedCornerShape(16.dp)) {
        Column(Modifier.fillMaxWidth().padding(15.dp), verticalArrangement = Arrangement.spacedBy(10.dp), content = content)
    }
}

@Composable
private fun SectionTitle(text: String) { Text(text, color = Muted, fontSize = 11.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.4.sp) }

@Composable
private fun MetricCard(title: String, value: String, note: String, modifier: Modifier = Modifier) {
    Card(modifier = modifier, colors = CardDefaults.cardColors(containerColor = Panel), shape = RoundedCornerShape(15.dp)) {
        Column(Modifier.padding(13.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Text(title, color = Muted, fontSize = 11.sp); Text(value, fontSize = 20.sp, fontWeight = FontWeight.Bold); Text(note, color = Muted, fontSize = 10.sp, lineHeight = 14.sp)
        }
    }
}

@Composable
private fun SafetyRow(label: String, status: String, color: Color) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
        Text(label, fontSize = 12.sp, modifier = Modifier.weight(1f)); Text(status, color = color, fontSize = 10.sp, fontWeight = FontWeight.Bold)
    }
}

@Composable
private fun StatusLine(symbol: String, text: String, color: Color) { Row(verticalAlignment = Alignment.CenterVertically) { Text(symbol, color = color); Text("  $text", color = color, fontSize = 11.sp, fontWeight = FontWeight.Bold) } }

@Composable
private fun ChoiceChip(text: String, selected: Boolean, onClick: () -> Unit) {
    Box(Modifier.background(if (selected) Gold.copy(alpha=.17f) else PanelAlt, RoundedCornerShape(9.dp)).border(1.dp, if (selected) Gold else Grid, RoundedCornerShape(9.dp)).clickable(onClick = onClick).padding(horizontal=11.dp, vertical=8.dp)) {
        Text(text, color=if(selected) Gold else Muted, fontSize=11.sp, fontWeight=FontWeight.SemiBold)
    }
}

@Composable
private fun Tag(text: String, color: Color) { Text(text, color=color, fontSize=9.sp, fontWeight=FontWeight.Bold, modifier=Modifier.background(color.copy(alpha=.12f), RoundedCornerShape(6.dp)).padding(horizontal=7.dp, vertical=5.dp)) }

@Composable
private fun LegendDot(label: String, color: Color) { Row(verticalAlignment=Alignment.CenterVertically, horizontalArrangement=Arrangement.spacedBy(4.dp)) { Text("●", color=color, fontSize=8.sp); Text(label, color=Muted, fontSize=8.sp) } }

@Composable
private fun IndicatorRow(name: String, value: String) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement=Arrangement.SpaceBetween, verticalAlignment=Alignment.CenterVertically) {
        Text(name, color=White, fontSize=12.sp); Text(value, color=Muted, fontSize=10.sp)
    }
}

@Composable
private fun AnalysisValue(label: String, value: String, color: Color) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement=Arrangement.SpaceBetween, verticalAlignment=Alignment.CenterVertically) {
        Text(label, color=Muted, fontSize=12.sp); Text(value, color=color, fontSize=11.sp, fontWeight=FontWeight.SemiBold)
    }
}
