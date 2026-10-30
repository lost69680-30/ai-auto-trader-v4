package com.aiautotrader.v4

import org.json.JSONObject
import okhttp3.OkHttpClient
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.tls.HandshakeCertificates
import okhttp3.tls.HeldCertificate
import org.junit.Assert.assertTrue
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test
import java.time.Instant

class MarketWebSocketClientTest {
    private val now = Instant.parse("2026-10-30T05:00:00Z")

    @Test
    fun websocketEndpointIsDerivedOnlyFromHttpsBackendBaseUrl() {
        assertEquals(
            "wss://solo-futures-websocket-staging.onrender.com/ws/v1/markets",
            marketWebSocketUrl("https://solo-futures-websocket-staging.onrender.com/"),
        )
        assertNull(marketWebSocketUrl("http://solo-futures-websocket-staging.onrender.com"))
        assertNull(marketWebSocketUrl("https://user:pass@example.com"))
        assertNull(marketWebSocketUrl(""))
    }

    @Test
    fun authenticatedRequestKeepsBearerTokenInHeaderAndUsesGetOnly() {
        val token = "unit-test-bearer-token-with-more-than-thirty-two-characters"
        val request = authenticatedMarketWebSocketRequest(
            "https://solo-futures-websocket-staging.onrender.com",
            token,
        )

        assertNotNull(request)
        assertEquals("GET", request!!.method)
        assertEquals("/ws/v1/markets", request.url.encodedPath)
        assertEquals("Bearer $token", request.header("Authorization"))
        assertNull(request.url.query)
    }

    @Test
    fun acceptsAuthenticatedReadOnlyFuturesMarketSnapshot() {
        val event = JSONObject()
            .put("schema_version", 1)
            .put("type", "market.snapshot")
            .put("symbol", "BTCUSDT")
            .put("market_type", "FUTURES")
            .put("observed_at", "2026-10-30T04:59:58Z")
            .put("status", "HEALTHY")
            .put("orders_authorized", false)
            .put("mark_price", 67000.0)
            .put("index_price", 66990.0)
            .put("funding_rate", 0.0001)
            .put("next_funding_at", "2026-10-30T08:00:00Z")

        val snapshot = parseMarketSnapshotEvent(event.toString(), now = now)

        assertNotNull(snapshot)
        assertEquals("BTCUSDT", snapshot!!.symbol)
        assertEquals(67000.0, snapshot.price, 0.0)
        assertEquals("FUTURES", snapshot.marketContext.marketType)
        assertEquals(66990.0, snapshot.marketContext.indexPrice!!, 0.0)
        assertEquals(0.0001, snapshot.marketContext.fundingRate!!, 0.0)
    }

    @Test
    fun acceptsSpotSnapshotWithoutFuturesOnlyFields() {
        val event = JSONObject()
            .put("schema_version", 1)
            .put("type", "market.snapshot")
            .put("symbol", "ETHUSDT")
            .put("market_type", "SPOT")
            .put("observed_at", "2026-10-30T04:59:58Z")
            .put("status", "HEALTHY")
            .put("orders_authorized", false)
            .put("last_price", 3000.0)

        val snapshot = parseMarketSnapshotEvent(event.toString(), now = now)

        assertNotNull(snapshot)
        assertEquals("SPOT", snapshot!!.marketContext.marketType)
        assertEquals(3000.0, snapshot.price, 0.0)
        assertNull(snapshot.marketContext.indexPrice)
        assertNull(snapshot.marketContext.fundingRate)
    }

    @Test
    fun rejectsUnsafeWrongSchemaAndUnknownMarketEvents() {
        val base = JSONObject()
            .put("schema_version", 1)
            .put("type", "market.snapshot")
            .put("symbol", "BTCUSDT")
            .put("market_type", "FUTURES")
            .put("observed_at", "2026-10-30T04:59:58Z")
            .put("status", "HEALTHY")
            .put("orders_authorized", false)
            .put("mark_price", 67000.0)
            .put("index_price", 66990.0)
            .put("funding_rate", 0.0001)
            .put("next_funding_at", "2026-10-30T08:00:00Z")

        assertNull(parseMarketSnapshotEvent(JSONObject(base.toString()).put("orders_authorized", true).toString(), now))
        assertNull(parseMarketSnapshotEvent(JSONObject(base.toString()).put("schema_version", 2).toString(), now))
        assertNull(parseMarketSnapshotEvent(JSONObject(base.toString()).put("type", "order.update").toString(), now))
        assertNull(parseMarketSnapshotEvent(JSONObject(base.toString()).put("market_type", "UNKNOWN").toString(), now))
    }

    @Test
    fun rejectsStaleOrIncompleteFuturesStreamSnapshots() {
        val base = JSONObject()
            .put("schema_version", 1)
            .put("type", "market.snapshot")
            .put("symbol", "BTCUSDT")
            .put("market_type", "FUTURES")
            .put("observed_at", "2026-10-30T04:59:58Z")
            .put("status", "HEALTHY")
            .put("orders_authorized", false)
            .put("mark_price", 67000.0)
            .put("index_price", 66990.0)
            .put("funding_rate", 0.0001)
            .put("next_funding_at", "2026-10-30T08:00:00Z")

        assertNull(parseMarketSnapshotEvent(JSONObject(base.toString()).put("index_price", JSONObject.NULL).toString(), now))
        assertNull(parseMarketSnapshotEvent(JSONObject(base.toString()).put("next_funding_at", "2026-10-30T04:00:00Z").toString(), now))
        assertNull(parseMarketSnapshotEvent(JSONObject(base.toString()).put("observed_at", "2026-10-30T04:00:00Z").toString(), now))
    }

    @Test
    fun reconnectDelayGrowsExponentiallyAndIsCapped() {
        assertEquals(1000L, websocketReconnectDelayMs(0))
        assertEquals(2000L, websocketReconnectDelayMs(1))
        assertEquals(4000L, websocketReconnectDelayMs(2))
        assertEquals(60_000L, websocketReconnectDelayMs(20))
    }
    @Test
    fun authenticatedWebSocketReceivesValidatedSnapshotAndSendsIgnoredKeepalive() {
        val server = MockWebServer()
        val heldCertificate = HeldCertificate.Builder()
            .addSubjectAlternativeName("localhost")
            .build()
        val serverCertificates = HandshakeCertificates.Builder()
            .heldCertificate(heldCertificate)
            .build()
        server.useHttps(serverCertificates.sslSocketFactory(), false)
        server.start()

        val clientCertificates = HandshakeCertificates.Builder()
            .addTrustedCertificate(heldCertificate.certificate)
            .build()
        val httpClient = OkHttpClient.Builder()
            .sslSocketFactory(clientCertificates.sslSocketFactory(), clientCertificates.trustManager)
            .build()

        val snapshotReceived = CountDownLatch(1)
        val keepaliveReceived = CountDownLatch(1)
        val receivedSnapshot = AtomicReference<MarketStreamSnapshot?>()
        val observedAt = Instant.now()
        val nextFundingAt = observedAt.plusSeconds(3600)
        val event = JSONObject()
            .put("schema_version", 1)
            .put("type", "market.snapshot")
            .put("symbol", "BTCUSDT")
            .put("market_type", "FUTURES")
            .put("observed_at", observedAt.toString())
            .put("status", "HEALTHY")
            .put("orders_authorized", false)
            .put("mark_price", 67000.0)
            .put("index_price", 66990.0)
            .put("funding_rate", 0.0001)
            .put("next_funding_at", nextFundingAt.toString())

        server.enqueue(
            MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
                override fun onOpen(webSocket: WebSocket, response: Response) {
                    webSocket.send(event.toString())
                }

                override fun onMessage(webSocket: WebSocket, text: String) {
                    if (text == """{"type":"keepalive"}""") {
                        keepaliveReceived.countDown()
                    }
                }
            }),
        )

        val stream = MarketWebSocketClient(
            onStatus = {},
            onSnapshot = {
                receivedSnapshot.set(it)
                snapshotReceived.countDown()
            },
            onUnauthorized = {},
            okHttpClient = httpClient,
            keepaliveIntervalMs = 25L,
        )
        try {
            stream.start("https://localhost:${server.port}", "unit-test-bearer-token-with-more-than-thirty-two-characters")
            val request = server.takeRequest(3, TimeUnit.SECONDS)
            assertNotNull(request)
            assertEquals("/ws/v1/markets", request!!.path)
            assertEquals(
                "Bearer unit-test-bearer-token-with-more-than-thirty-two-characters",
                request.getHeader("Authorization"),
            )
            assertTrue("snapshot should arrive over the WebSocket", snapshotReceived.await(3, TimeUnit.SECONDS))
            assertTrue("client should send the server-ignored keepalive", keepaliveReceived.await(3, TimeUnit.SECONDS))
            assertEquals("BTCUSDT", receivedSnapshot.get()?.symbol)
            assertEquals(67000.0, receivedSnapshot.get()!!.price, 0.0)
            assertEquals("FUTURES", receivedSnapshot.get()!!.marketContext.marketType)
        } finally {
            stream.close()
            server.shutdown()
            httpClient.dispatcher.executorService.shutdown()
            httpClient.connectionPool.evictAll()
        }
    }


    @Test
    fun reconnectsWhenSocketStaysOpenButValidSnapshotsStopArriving() {
        val server = MockWebServer()
        val heldCertificate = HeldCertificate.Builder()
            .addSubjectAlternativeName("localhost")
            .build()
        val serverCertificates = HandshakeCertificates.Builder()
            .heldCertificate(heldCertificate)
            .build()
        server.useHttps(serverCertificates.sslSocketFactory(), false)
        server.start()

        val clientCertificates = HandshakeCertificates.Builder()
            .addTrustedCertificate(heldCertificate.certificate)
            .build()
        val httpClient = OkHttpClient.Builder()
            .sslSocketFactory(clientCertificates.sslSocketFactory(), clientCertificates.trustManager)
            .build()
        val reconnecting = CountDownLatch(1)
        val stream = MarketWebSocketClient(
            onStatus = { if (it == "RECONNECTING") reconnecting.countDown() },
            onSnapshot = {},
            onUnauthorized = {},
            okHttpClient = httpClient,
            keepaliveIntervalMs = 10_000L,
            snapshotTimeoutMs = 200L,
        )
        repeat(2) {
            server.enqueue(
                MockResponse().withWebSocketUpgrade(object : WebSocketListener() {}),
            )
        }

        try {
            stream.start(
                "https://localhost:${server.port}",
                "unit-test-bearer-token-with-more-than-thirty-two-characters",
            )
            assertNotNull(server.takeRequest(3, TimeUnit.SECONDS))
            assertTrue(
                "quiet but open socket must be marked reconnecting",
                reconnecting.await(3, TimeUnit.SECONDS),
            )
            assertNotNull(
                "watchdog must cause a new connection attempt",
                server.takeRequest(3, TimeUnit.SECONDS),
            )
        } finally {
            stream.close()
            server.shutdown()
            httpClient.dispatcher.executorService.shutdown()
            httpClient.connectionPool.evictAll()
        }
    }

}
