package com.aiautotrader.v4

import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.io.Closeable
import java.time.Duration
import java.time.Instant
import java.time.OffsetDateTime
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit

private const val MARKET_WEBSOCKET_PATH = "/ws/v1/markets"
private const val MARKET_SNAPSHOT_MAX_AGE_SECONDS = 90.0
private const val DEFAULT_KEEPALIVE_INTERVAL_MS = 25_000L
private const val BASE_RECONNECT_DELAY_MS = 1_000L
private const val MAX_RECONNECT_DELAY_MS = 60_000L
private const val KEEPALIVE_MESSAGE = """{"type":"keepalive"}"""

internal data class MarketStreamSnapshot(
    val symbol: String,
    val price: Double,
    val observedAt: String,
    val marketContext: MarketDataContext,
)

/**
 * Build the WebSocket endpoint only from the configured HTTPS backend origin.
 * User-provided URLs, credentials, query parameters, fragments and path prefixes
 * are rejected so the bearer token cannot be sent to an arbitrary host/path.
 */
internal fun marketWebSocketUrl(apiBaseUrl: String): String? {
    val parsed = apiBaseUrl.trim().toHttpUrlOrNull() ?: return null
    if (!parsed.isHttps ||
        parsed.username.isNotEmpty() ||
        parsed.password.isNotEmpty() ||
        parsed.encodedPath != "/" ||
        parsed.encodedQuery != null ||
        parsed.fragment != null
    ) {
        return null
    }
    val httpsOrigin = parsed.newBuilder()
        .encodedPath("/")
        .query(null)
        .fragment(null)
        .build()
        .toString()
        .removeSuffix("/")
    return httpsOrigin.replaceFirst("https://", "wss://") + MARKET_WEBSOCKET_PATH
}

internal fun websocketReconnectDelayMs(
    attempt: Int,
    baseDelayMs: Long = BASE_RECONNECT_DELAY_MS,
    maxDelayMs: Long = MAX_RECONNECT_DELAY_MS,
): Long {
    val safeBase = baseDelayMs.coerceAtLeast(1L)
    val safeMax = maxDelayMs.coerceAtLeast(safeBase)
    val exponent = attempt.coerceAtLeast(0).coerceAtMost(20)
    val multiplier = 1L shl exponent
    val delay = if (safeBase > safeMax / multiplier) safeMax else safeBase * multiplier
    return delay.coerceAtMost(safeMax)
}

/**
 * Parse the backend's versioned market.snapshot contract. Only explicitly
 * healthy snapshots with orders_authorized=false and fresh price/context data
 * are allowed to update the UI.
 */
internal fun parseMarketSnapshotEvent(
    text: String,
    now: Instant = Instant.now(),
    maxAgeSeconds: Double = MARKET_SNAPSHOT_MAX_AGE_SECONDS,
): MarketStreamSnapshot? {
    if (!maxAgeSeconds.isFinite() || maxAgeSeconds <= 0.0) return null
    val event = try {
        JSONObject(text)
    } catch (_: Exception) {
        return null
    }

    if (event.optInt("schema_version", -1) != 1 ||
        event.optString("type") != "market.snapshot" ||
        event.optString("status") != "HEALTHY" ||
        event.optBoolean("orders_authorized", true)
    ) {
        return null
    }

    val symbol = event.optString("symbol")
    if (!symbol.matches(Regex("^[A-Z0-9]{2,24}$"))) return null

    val marketType = event.optString("market_type")
    val priceType = when (marketType) {
        "FUTURES" -> "FUTURES_MARK_PRICE"
        "SPOT" -> "SPOT_LAST_PRICE"
        else -> return null
    }
    val priceField = if (marketType == "FUTURES") "mark_price" else "last_price"
    val price = event.optDouble(priceField, Double.NaN)
    if (!price.isFinite() || price <= 0.0) return null

    val observedAt = event.optString("observed_at")
    val observedInstant = try {
        OffsetDateTime.parse(observedAt).toInstant()
    } catch (_: Exception) {
        return null
    }
    val ageSeconds = Duration.between(observedInstant, now).toMillis() / 1000.0
    if (ageSeconds < -1.0 || ageSeconds > maxAgeSeconds) return null

    val indexPrice = if (event.isNull("index_price")) {
        null
    } else {
        event.optDouble("index_price", Double.NaN)
    }
    val fundingRate = if (event.isNull("funding_rate")) {
        null
    } else {
        event.optDouble("funding_rate", Double.NaN)
    }
    val nextFundingAt = if (event.isNull("next_funding_at")) {
        null
    } else {
        event.optString("next_funding_at").takeIf { it.isNotBlank() && it != "null" }
    }
    val marketContext = validateMarketDataContext(
        marketType = marketType,
        priceType = priceType,
        indexPrice = indexPrice,
        fundingRate = fundingRate,
        nextFundingAt = nextFundingAt,
        now = now,
    ) ?: return null

    return MarketStreamSnapshot(
        symbol = symbol,
        price = price,
        observedAt = observedInstant.toString(),
        marketContext = marketContext,
    )
}

internal class MarketWebSocketClient(
    private val onStatus: (String) -> Unit,
    private val onSnapshot: (MarketStreamSnapshot) -> Unit,
    private val onUnauthorized: () -> Unit,
    private val okHttpClient: OkHttpClient = defaultMarketWebSocketClient(),
    private val keepaliveIntervalMs: Long = DEFAULT_KEEPALIVE_INTERVAL_MS,
) : Closeable {
    private val scheduler: ScheduledExecutorService =
        Executors.newSingleThreadScheduledExecutor { task ->
            Thread(task, "solo-market-websocket").apply { isDaemon = true }
        }
    private val lock = Any()

    @Volatile
    private var running = false
    private var endpoint = ""
    private var bearerToken = ""
    private var reconnectAttempts = 0
    private var socket: WebSocket? = null
    private var reconnectTask: ScheduledFuture<*>? = null
    private var keepaliveTask: ScheduledFuture<*>? = null

    fun start(apiBaseUrl: String, token: String) {
        val safeEndpoint = marketWebSocketUrl(apiBaseUrl)
        if (token.length < 32 || safeEndpoint == null || !safeEndpoint.startsWith("wss://")) {
            onStatus("UNAVAILABLE")
            return
        }
        synchronized(lock) {
            running = true
            endpoint = safeEndpoint
            bearerToken = token
            reconnectAttempts = 0
        }
        onStatus("CONNECTING")
        scheduleConnect(0L)
    }

    private fun scheduleConnect(delayMs: Long) {
        synchronized(lock) {
            if (!running || reconnectTask != null) return
            reconnectTask = scheduler.schedule({
                synchronized(lock) { reconnectTask = null }
                connectNow()
            }, delayMs, TimeUnit.MILLISECONDS)
        }
    }

    private fun connectNow() {
        val requestData = synchronized(lock) {
            if (!running) return
            endpoint to bearerToken
        }
        onStatus(if (reconnectAttempts == 0) "CONNECTING" else "RECONNECTING")
        val request = Request.Builder()
            .url(requestData.first)
            .header("Authorization", "Bearer ${requestData.second}")
            .header("Accept", "application/json")
            .build()

        val createdSocket = okHttpClient.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                synchronized(lock) {
                    if (!running) {
                        webSocket.close(1000, "app not foreground")
                        return
                    }
                    socket = webSocket
                    reconnectAttempts = 0
                    reconnectTask?.cancel(false)
                    reconnectTask = null
                    keepaliveTask?.cancel(false)
                    keepaliveTask = scheduler.scheduleAtFixedRate({
                        val isActive = synchronized(lock) { running && socket === webSocket }
                        if (isActive && !webSocket.send(KEEPALIVE_MESSAGE)) {
                            webSocket.cancel()
                        }
                    }, keepaliveIntervalMs, keepaliveIntervalMs, TimeUnit.MILLISECONDS)
                }
                onStatus("CONNECTED")
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val snapshot = parseMarketSnapshotEvent(text) ?: return
                onSnapshot(snapshot)
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                onStatus("RECONNECTING")
                webSocket.close(code, null)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                scheduleReconnect(webSocket)
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                if (response?.code == 401 || response?.code == 403) {
                    synchronized(lock) {
                        if (socket === webSocket) socket = null
                        running = false
                        reconnectTask?.cancel(false)
                        reconnectTask = null
                        keepaliveTask?.cancel(false)
                        keepaliveTask = null
                    }
                    onStatus("UNAUTHORIZED")
                    onUnauthorized()
                } else {
                    scheduleReconnect(webSocket)
                }
            }
        })

        synchronized(lock) {
            if (running) {
                socket = createdSocket
            } else {
                createdSocket.cancel()
            }
        }
    }

    private fun scheduleReconnect(failedSocket: WebSocket) {
        val delayMs: Long
        synchronized(lock) {
            if (!running) return
            if (socket === failedSocket) socket = null
            keepaliveTask?.cancel(false)
            keepaliveTask = null
            if (reconnectTask != null) return
            delayMs = websocketReconnectDelayMs(reconnectAttempts++)
        }
        onStatus("RECONNECTING")
        scheduleConnect(delayMs)
    }

    private fun defaultMarketWebSocketClient(): OkHttpClient = okHttpClient

    override fun close() {
        synchronized(lock) {
            running = false
            reconnectTask?.cancel(false)
            reconnectTask = null
            keepaliveTask?.cancel(false)
            keepaliveTask = null
            val activeSocket = socket
            socket = null
            activeSocket?.close(1000, "app not foreground")
            bearerToken = ""
        }
        onStatus("PAUSED")
        scheduler.shutdownNow()
    }

    companion object {
        private fun defaultMarketWebSocketClient(): OkHttpClient =
            sharedClient

        private val sharedClient: OkHttpClient by lazy {
            OkHttpClient.Builder()
                .pingInterval(25, TimeUnit.SECONDS)
                .build()
        }
    }
}
