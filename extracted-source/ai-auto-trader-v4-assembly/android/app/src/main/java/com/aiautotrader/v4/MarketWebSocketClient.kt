package com.aiautotrader.v4

import android.os.Handler
import android.os.Looper
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.time.Duration
import java.time.Instant
import java.util.concurrent.TimeUnit

internal data class MarketStreamSnapshot(
    val symbol: String,
    val marketType: String,
    val price: Double,
    val observedAt: Instant,
    val indexPrice: Double?,
    val fundingRate: Double?,
    val nextFundingAt: Instant?,
)

internal fun parseMarketStreamEvent(
    raw: String,
    now: Instant = Instant.now(),
    maxAgeSeconds: Long = 90,
): MarketStreamSnapshot? = try {
    val json = JSONObject(raw)
    if (json.optInt("schema_version", -1) != 1 ||
        json.optString("type") != "market.snapshot" ||
        json.optString("status") != "HEALTHY" ||
        json.optBoolean("orders_authorized", true)
    ) return null

    val symbol = json.optString("symbol")
    if (!Regex("[A-Z0-9]{3,24}").matches(symbol)) return null

    val marketType = json.optString("market_type")
    val priceField = if (marketType == "SPOT") "last_price" else if (marketType == "FUTURES") "mark_price" else return null
    val price = json.optDouble(priceField, Double.NaN)
    if (!price.isFinite() || price <= 0.0) return null

    val observed = Instant.parse(json.optString("observed_at"))
    val age = Duration.between(observed, now).toMillis()
    if (age < -1_000L || age > maxAgeSeconds * 1_000L) return null

    if (marketType == "SPOT") {
        if (json.has("index_price") || json.has("funding_rate") || json.has("next_funding_at")) return null
        MarketStreamSnapshot(symbol, marketType, price, observed, null, null, null)
    } else {
        val index = json.optDouble("index_price", Double.NaN)
        val funding = json.optDouble("funding_rate", Double.NaN)
        if (!index.isFinite() || index <= 0.0 || !funding.isFinite()) return null
        val nextFunding = Instant.parse(json.optString("next_funding_at"))
        if (!nextFunding.isAfter(now)) return null
        MarketStreamSnapshot(symbol, marketType, price, observed, index, funding, nextFunding)
    }
} catch (_: Exception) {
    null
}

internal fun marketStreamUrl(httpsBaseUrl: String): String? {
    val base = httpsBaseUrl.trimEnd('/')
    if (!base.startsWith("https://") || base.length <= "https://".length) return null
    return "wss://" + base.removePrefix("https://") + "/ws/v1/markets"
}

internal fun marketStreamReconnectDelayMillis(attempt: Int): Long {
    val exponent = attempt.coerceIn(0, 5)
    return (1_000L shl exponent).coerceAtMost(30_000L)
}

/** Authenticated one-way market feed. It never sends client messages or order commands. */
internal class MarketWebSocketClient(
    private val baseUrl: String,
    private val token: String,
    private val onStatus: (String) -> Unit,
    private val onSnapshot: (MarketStreamSnapshot) -> Unit,
) {
    private val handler = Handler(Looper.getMainLooper())
    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .pingInterval(25, TimeUnit.SECONDS)
        .build()
    private var socket: WebSocket? = null
    private var reconnectAttempt = 0
    @Volatile private var stopped = false
    private var reconnectTask: Runnable? = null

    fun start() {
        if (token.length < 32 || marketStreamUrl(baseUrl) == null) {
            onStatus("NOT CONFIGURED")
            return
        }
        stopped = false
        connect()
    }

    fun close() {
        stopped = true
        reconnectTask?.let(handler::removeCallbacks)
        reconnectTask = null
        socket?.close(1000, "screen stopped")
        socket = null
        client.dispatcher.executorService.shutdown()
        onStatus("DISCONNECTED")
    }

    private fun connect() {
        if (stopped) return
        val url = marketStreamUrl(baseUrl) ?: return
        onStatus("CONNECTING")
        val request = Request.Builder()
            .url(url)
            .header("Authorization", "Bearer $token")
            .header("Accept", "application/json")
            .build()
        socket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                reconnectAttempt = 0
                onStatus("CONNECTED")
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                parseMarketStreamEvent(text)?.let(onSnapshot)
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(code, null)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                scheduleReconnect()
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                onStatus("RECONNECTING")
                scheduleReconnect()
            }
        })
    }

    private fun scheduleReconnect() {
        if (stopped || reconnectTask != null) return
        val delay = marketStreamReconnectDelayMillis(reconnectAttempt++)
        val task = Runnable {
            reconnectTask = null
            connect()
        }
        reconnectTask = task
        handler.postDelayed(task, delay)
    }
}
