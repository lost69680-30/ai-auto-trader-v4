package com.aiautotrader.v4

import org.json.JSONObject
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
}
