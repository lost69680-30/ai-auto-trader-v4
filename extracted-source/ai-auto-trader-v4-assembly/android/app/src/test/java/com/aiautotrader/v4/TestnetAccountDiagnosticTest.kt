package com.aiautotrader.v4

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class TestnetAccountDiagnosticTest {
    @Test
    fun allowsAuthenticatedMarketRequestsToSurviveFreeTierColdStarts() {
        assertEquals(75_000, BACKEND_MARKET_READ_TIMEOUT_MS)
    }

    @Test
    fun recognizesOnlySuccessfulReadOnlyTestnetAccountResponses() {
        val result = classifyTestnetAccountResponse(
            httpCode = 200,
            mode = "BINANCE_SPOT_TESTNET_READ_ONLY",
            connected = true,
            ordersAuthorized = false,
            upstreamStatus = "CONNECTED",
            balanceCount = 3
        )

        assertEquals("CONNECTED", result.status)
        assertTrue(result.details.contains("3"))
        assertTrue(result.details.contains("disabled"))
    }

    @Test
    fun keepsAccountFailureSeparateFromBackendAvailability() {
        val result = classifyTestnetAccountResponse(
            httpCode = 503,
            mode = "BINANCE_SPOT_TESTNET_READ_ONLY",
            connected = false,
            ordersAuthorized = false,
            upstreamStatus = "BINANCE_HTTP_451",
            balanceCount = 0
        )

        assertEquals("UNAVAILABLE", result.status)
        assertTrue(result.details.contains("451"))
    }

    @Test
    fun reportsMissingExchangeCredentialsWithoutCallingAccountConnected() {
        val result = classifyTestnetAccountResponse(
            httpCode = 503,
            mode = "BINANCE_SPOT_TESTNET_READ_ONLY",
            connected = false,
            ordersAuthorized = false,
            upstreamStatus = "NOT_CONFIGURED",
            balanceCount = 0
        )

        assertEquals("NOT CONFIGURED", result.status)
    }

    @Test
    fun blocksAnyAccountPayloadThatClaimsOrderAuthorization() {
        val result = classifyTestnetAccountResponse(
            httpCode = 200,
            mode = "BINANCE_SPOT_TESTNET_READ_ONLY",
            connected = true,
            ordersAuthorized = true,
            upstreamStatus = "CONNECTED",
            balanceCount = 3
        )

        assertEquals("BLOCKED", result.status)
        assertTrue(result.details.contains("rejected"))
    }

    @Test
    fun boundsUnknownUpstreamDiagnosticText() {
        val result = classifyTestnetAccountResponse(
            httpCode = 503,
            mode = "BINANCE_SPOT_TESTNET_READ_ONLY",
            connected = false,
            ordersAuthorized = false,
            upstreamStatus = "X".repeat(1000),
            balanceCount = 0
        )

        assertEquals("UNAVAILABLE", result.status)
        assertTrue(result.details.length <= 180)
    }
}
