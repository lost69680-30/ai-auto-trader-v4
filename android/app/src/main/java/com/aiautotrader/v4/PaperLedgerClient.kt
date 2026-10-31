package com.aiautotrader.v4

import java.net.HttpURLConnection
import java.net.URL
import org.json.JSONArray
import org.json.JSONObject

internal data class PaperPositionView(
    val proposalId: String,
    val symbol: String,
    val side: String,
    val quantity: Double,
    val entryPrice: Double,
    val markPrice: Double,
    val stopLoss: Double,
    val takeProfit: Double,
    val status: String,
    val unrealizedPnl: Double,
    val totalPnl: Double,
    val markObservedAt: String,
)

internal data class PaperOrderView(
    val proposalId: String,
    val symbol: String,
    val side: String,
    val quantity: Double,
    val entryPrice: Double,
    val stopLoss: Double,
    val takeProfit: Double,
    val status: String,
    val recordedAt: String,
)

internal data class PaperRiskView(
    val emergencyStopActive: Boolean,
    val openPositionCount: Int,
    val accountEquity: Double?,
    val drawdownAmount: Double?,
    val drawdownPct: Double?,
    val unrealizedPnl: Double?,
    val marksFresh: Boolean?,
    val snapshotAt: String?,
    val stateStorage: String,
)

internal data class PaperAuditView(
    val eventType: String,
    val proposalId: String,
    val occurredAt: String,
    val summary: String,
)

internal data class PaperLedgerSnapshot(
    val positions: List<PaperPositionView>,
    val orders: List<PaperOrderView>,
    val risk: PaperRiskView,
    val audit: List<PaperAuditView>,
    val performanceClosedCount: Int,
    val performanceRealizedPnl: Double,
    val fetchedAt: String,
)

internal fun parsePaperPositions(json: JSONObject): List<PaperPositionView> {
    require(json.optBoolean("orders_authorized", true).not()) { "Unsafe positions response" }
    val rows = json.optJSONArray("items") ?: throw IllegalArgumentException("Positions response has no items array")
    return (0 until rows.length()).mapNotNull { index ->
        val row = rows.optJSONObject(index) ?: return@mapNotNull null
        val quantity = row.optDouble("quantity", Double.NaN)
        val entry = row.optDouble("entry_price", Double.NaN)
        val mark = row.optDouble("mark_price", Double.NaN)
        val stop = row.optDouble("stop_loss", Double.NaN)
        val target = row.optDouble("take_profit", Double.NaN)
        val unrealized = row.optDouble("unrealized_pnl", Double.NaN)
        val total = row.optDouble("total_pnl", Double.NaN)
        val symbol = row.optString("symbol").trim()
        val side = row.optString("side").uppercase()
        val status = row.optString("status").uppercase()
        if (symbol.isBlank() || side !in setOf("LONG", "SHORT") ||
            status !in setOf("OPEN", "CLOSED") ||
            listOf(quantity, entry, mark, stop, target, unrealized, total).any { !it.isFinite() } ||
            quantity <= 0.0 || entry <= 0.0 || mark <= 0.0 || stop <= 0.0 || target <= 0.0
        ) return@mapNotNull null
        PaperPositionView(
            row.optString("proposal_id"), symbol, side, quantity, entry, mark, stop, target,
            status, unrealized, total, row.optString("mark_observed_at")
        )
    }
}

internal fun parsePaperOrders(json: JSONObject): List<PaperOrderView> {
    require(!json.optBoolean("orders_authorized", true)) { "Unsafe orders response" }
    val rows = json.optJSONArray("items") ?: throw IllegalArgumentException("Orders response has no items array")
    return (0 until rows.length()).mapNotNull { index ->
        val row = rows.optJSONObject(index) ?: return@mapNotNull null
        val quantity = row.optDouble("quantity", Double.NaN)
        val entry = row.optDouble("entry_price", Double.NaN)
        val stop = row.optDouble("stop_loss", Double.NaN)
        val target = row.optDouble("take_profit", Double.NaN)
        val symbol = row.optString("symbol").trim()
        val side = row.optString("side").uppercase()
        if (symbol.isBlank() || side !in setOf("LONG", "SHORT") ||
            listOf(quantity, entry, stop, target).any { !it.isFinite() } ||
            quantity <= 0.0 || entry <= 0.0 || stop <= 0.0 || target <= 0.0
        ) return@mapNotNull null
        PaperOrderView(row.optString("proposal_id"), symbol, side, quantity, entry, stop, target,
            row.optString("status"), row.optString("recorded_at"))
    }
}

internal fun parsePaperRisk(json: JSONObject): PaperRiskView {
    require(!json.optBoolean("orders_authorized", true)) { "Unsafe risk response" }
    val snapshot = json.optJSONObject("latest_snapshot")
    fun finite(name: String): Double? {
        if (snapshot == null || snapshot.isNull(name)) return null
        val value = snapshot.optDouble(name, Double.NaN)
        return value.takeIf { it.isFinite() }
    }
    return PaperRiskView(
        emergencyStopActive = json.optBoolean("emergency_stop_active", false),
        openPositionCount = json.optInt("open_position_count", -1).takeIf { it >= 0 }
            ?: throw IllegalArgumentException("Invalid open position count"),
        accountEquity = finite("account_equity"),
        drawdownAmount = finite("drawdown_amount"),
        drawdownPct = finite("drawdown_pct"),
        unrealizedPnl = finite("unrealized_pnl"),
        marksFresh = if (snapshot == null || snapshot.isNull("marks_fresh")) null else snapshot.optBoolean("marks_fresh"),
        snapshotAt = snapshot?.optString("snapshot_at")?.takeIf { it.isNotBlank() && it != "null" },
        stateStorage = json.optString("state_storage", "UNKNOWN")
    )
}

internal fun parsePaperAudit(json: JSONObject): List<PaperAuditView> {
    require(!json.optBoolean("orders_authorized", true)) { "Unsafe audit response" }
    val rows = json.optJSONArray("items") ?: throw IllegalArgumentException("Audit response has no items array")
    return (0 until rows.length()).mapNotNull { index ->
        val row = rows.optJSONObject(index) ?: return@mapNotNull null
        val eventType = row.optString("event_type").trim()
        if (eventType.isBlank()) return@mapNotNull null
        val payload = row.optJSONObject("payload")
        val summary = payload?.toString()?.take(180).orEmpty()
        PaperAuditView(eventType, row.optString("proposal_id"), row.optString("occurred_at"), summary)
    }.takeLast(100).reversed()
}

/** Authenticated GET-only ledger reader. It never sends order or control commands. */
internal fun fetchPaperLedgerSnapshot(baseUrl: String, token: String): PaperLedgerSnapshot {
    require(baseUrl.startsWith("https://")) { "Authenticated HTTPS backend is required" }
    require(token.length >= 32) { "A valid backend token is required" }

    fun get(path: String): JSONObject {
        val connection = URL(baseUrl.trimEnd('/') + path).openConnection() as HttpURLConnection
        try {
            connection.requestMethod = "GET"
            connection.connectTimeout = 10_000
            connection.readTimeout = 20_000
            connection.setRequestProperty("Authorization", "Bearer $token")
            connection.setRequestProperty("Accept", "application/json")
            connection.useCaches = false
            val code = connection.responseCode
            val stream = if (code in 200..299) connection.inputStream else connection.errorStream
            val body = stream?.bufferedReader()?.use { it.readText() }.orEmpty()
            if (code !in 200..299) throw IllegalStateException("Ledger endpoint returned HTTP $code")
            return JSONObject(body)
        } finally {
            connection.disconnect()
        }
    }

    val positions = parsePaperPositions(get("/api/v1/positions"))
    val orders = parsePaperOrders(get("/api/v1/orders"))
    val risk = parsePaperRisk(get("/api/v1/risk"))
    val audit = parsePaperAudit(get("/api/v1/audit"))
    val closed = positions.filter { it.status == "CLOSED" }
    val realized = closed.sumOf { it.totalPnl }
    return PaperLedgerSnapshot(
        positions, orders, risk, audit, closed.size, realized,
        java.time.Instant.now().toString()
    )
}
