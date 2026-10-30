package com.aiautotrader.v4

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.time.Instant

class MarketStreamContractTest {
    private fun event(
        observedAt: String = Instant.now().toString(),
        ordersAuthorized: Boolean = false,
        status: String = "HEALTHY",
        price: Double = 65000.0,
    ) = JSONObject().apply {
        put("schema_version", 1)
        put("type", "market.snapshot")
        put("symbol", "BTCUSDT")
        put("market_type", "FUTURES")
        put("mark_price", price)
        put("index_price", 64990.0)
        put("funding_rate", 0.0001)
        put("next_funding_at", Instant.now().plusSeconds(3600).toString())
        put("observed_at", observedAt)
        put("status", status)
        put("orders_authorized", ordersAuthorized)
    }

    @Test fun acceptsFreshReadOnlyFuturesSnapshot() {
        val snapshot = parseMarketStreamEvent(event().toString(), Instant.now())
        assertNotNull(snapshot)
        assertEquals("BTCUSDT", snapshot!!.symbol)
        assertEquals(65000.0, snapshot.price, 0.0)
        assertEquals(64990.0, snapshot.indexPrice!!, 0.0)
        assertEquals(0.0001, snapshot.fundingRate!!, 0.0)
    }

    @Test fun rejectsWrongSchemaOrEventType() {
        val wrongVersion = event().put("schema_version", 2)
        val wrongType = event().put("type", "order.command")
        assertNull(parseMarketStreamEvent(wrongVersion.toString(), Instant.now()))
        assertNull(parseMarketStreamEvent(wrongType.toString(), Instant.now()))
    }

    @Test fun rejectsAnyOrderAuthorization() {
        assertNull(parseMarketStreamEvent(event(ordersAuthorized = true).toString(), Instant.now()))
    }

    @Test fun rejectsStaleFutureOrInvalidPrices() {
        val now = Instant.now()
        assertNull(parseMarketStreamEvent(event(observedAt = now.minusSeconds(91).toString()).toString(), now))
        assertNull(parseMarketStreamEvent(event(observedAt = now.plusSeconds(5).toString()).toString(), now))
        assertNull(parseMarketStreamEvent(event().put("mark_price", "NaN").toString(), now))
        assertNull(parseMarketStreamEvent(event(price = 0.0).toString(), now))
    }

    @Test fun rejectsUnhealthyOrMalformedSymbols() {
        assertNull(parseMarketStreamEvent(event(status = "OFFLINE").toString(), Instant.now()))
        assertNull(parseMarketStreamEvent(event().put("symbol", "btc/usdt").toString(), Instant.now()))
        assertNull(parseMarketStreamEvent("not-json", Instant.now()))
    }

    @Test fun convertsOnlyHttpsBackendUrlsToWebSocketUrls() {
        assertEquals(
            "wss://staging.example.com/ws/v1/markets",
            marketStreamUrl("https://staging.example.com/")
        )
        assertNull(marketStreamUrl("http://staging.example.com"))
        assertNull(marketStreamUrl("https://user:secret@staging.example.com"))
        assertNull(marketStreamUrl("https://staging.example.com/custom"))
        assertNull(marketStreamUrl("https://staging.example.com?token=unsafe"))
        assertNull(marketStreamUrl(""))
    }

    @Test fun rejectsUnsafeBackendOriginsBeforeAttachingBearerCredentials() {
        assertNull(marketStreamUrl("https://attacker.example/path"))
        assertNull(marketStreamUrl("https://staging.example.com/#fragment"))
    }

    @Test fun reconnectDelayIsCappedAndExponential() {
        assertEquals(1_000L, marketStreamReconnectDelayMillis(0))
        assertEquals(2_000L, marketStreamReconnectDelayMillis(1))
        assertEquals(4_000L, marketStreamReconnectDelayMillis(2))
        assertEquals(30_000L, marketStreamReconnectDelayMillis(20))
    }
}
