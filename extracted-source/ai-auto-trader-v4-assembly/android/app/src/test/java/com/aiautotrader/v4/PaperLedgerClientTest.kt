package com.aiautotrader.v4

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class PaperLedgerClientTest {
    @Test
    fun parsesOnlyValidatedPaperPositions() {
        val json = JSONObject()
            .put("orders_authorized", false)
            .put("items", JSONArray()
                .put(JSONObject()
                    .put("proposal_id", "p-1").put("symbol", "BTCUSDT").put("side", "LONG")
                    .put("quantity", 0.01).put("entry_price", 60000.0).put("mark_price", 61000.0)
                    .put("stop_loss", 59000.0).put("take_profit", 63000.0).put("status", "OPEN")
                    .put("unrealized_pnl", 10.0).put("total_pnl", 9.0).put("mark_observed_at", "2026-10-31T00:00:00Z"))
                .put(JSONObject().put("symbol", "BAD").put("side", "BUY").put("status", "OPEN")))
        val positions = parsePaperPositions(json)
        assertEquals(1, positions.size)
        assertEquals("BTCUSDT", positions.single().symbol)
        assertEquals(10.0, positions.single().unrealizedPnl, 0.0)
    }

    @Test
    fun rejectsPositionsWhenAuthorizationFlagIsMissingOrUnsafe() {
        assertThrows(IllegalArgumentException::class.java) {
            parsePaperPositions(JSONObject().put("items", JSONArray()))
        }
        assertThrows(IllegalArgumentException::class.java) {
            parsePaperPositions(JSONObject().put("orders_authorized", true).put("items", JSONArray()))
        }
    }

    @Test
    fun parsesRiskSnapshotWithoutInventingMissingMetrics() {
        val json = JSONObject()
            .put("orders_authorized", false)
            .put("emergency_stop_active", true)
            .put("open_position_count", 2)
            .put("state_storage", "postgresql")
            .put("latest_snapshot", JSONObject().put("account_equity", 1000.0)
                .put("drawdown_pct", 1.5).put("marks_fresh", false).put("snapshot_at", "2026-10-31T00:00:00Z"))
        val risk = parsePaperRisk(json)
        assertTrue(risk.emergencyStopActive)
        assertEquals(2, risk.openPositionCount)
        assertEquals(1000.0, risk.accountEquity!!, 0.0)
        assertNull(risk.unrealizedPnl)
        assertFalse(risk.marksFresh!!)
    }

    @Test
    fun rejectsRiskResponseWithoutExplicitPaperOnlyFlag() {
        assertThrows(IllegalArgumentException::class.java) {
            parsePaperRisk(JSONObject().put("open_position_count", 0))
        }
    }

    @Test
    fun parsesAuditEventsAndLimitsHistory() {
        val json = JSONObject().put("orders_authorized", false).put("items", JSONArray()
            .put(JSONObject().put("event_type", "RISK_CHECK_BLOCKED").put("occurred_at", "2026-10-31T00:00:00Z")
                .put("payload", JSONObject().put("reason", "stale data"))))
        val events = parsePaperAudit(json)
        assertEquals(1, events.size)
        assertEquals("RISK_CHECK_BLOCKED", events.single().eventType)
        assertTrue(events.single().summary.contains("stale data"))
    }
}
