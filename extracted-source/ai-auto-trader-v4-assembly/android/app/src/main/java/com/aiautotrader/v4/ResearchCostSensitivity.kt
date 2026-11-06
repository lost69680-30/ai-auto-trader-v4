package com.aiautotrader.v4

import org.json.JSONArray
import org.json.JSONObject

internal data class ResearchCostScenario(
    val multiplier: Double,
    val foldCount: Int,
    val tradeCount: Int,
    val totalEstimatedCosts: Double,
    val meanNetReturnPct: Double,
    val meanExcessReturnPct: Double,
    val positiveFoldPct: Double,
    val maximumFoldDrawdownPct: Double,
)

internal data class ResearchCostSensitivity(
    val status: String,
    val reason: String?,
    val scenarios: List<ResearchCostScenario>,
)

internal fun parseResearchCostSensitivity(outcomes: JSONObject): ResearchCostSensitivity {
    val source = outcomes.optJSONObject("cost_sensitivity")
        ?: return ResearchCostSensitivity("MISSING", "No cost sensitivity report is available for this experiment.", emptyList())
    require(source.optBoolean("orders_authorized", true).not()) {
        "Cost sensitivity report did not explicitly disable order authorization"
    }
    require(source.optBoolean("holdout_used", true).not()) {
        "Cost sensitivity report must not use untouched holdout data"
    }
    val status = source.optString("status", "UNKNOWN").trim().uppercase()
    val reason = source.optString("reason").trim().takeIf { it.isNotBlank() && it != "null" }
    val array = source.optJSONArray("scenarios") ?: JSONArray()
    if (status == "UNAVAILABLE") {
        require(array.length() == 0) { "Unavailable cost sensitivity cannot contain scenario values" }
        return ResearchCostSensitivity(status, reason, emptyList())
    }
    require(status == "EVALUATED") { "Cost sensitivity status is invalid" }
    require(array.length() in 1..10) { "Cost sensitivity scenarios are missing or oversized" }
    val scenarios = (0 until array.length()).map { index ->
        val row = array.optJSONObject(index)
            ?: throw IllegalArgumentException("Cost sensitivity scenario is malformed")
        require(!row.optBoolean("orders_authorized", true)) {
            "Cost sensitivity scenario did not explicitly disable order authorization"
        }
        require(!row.optBoolean("holdout_used", true)) {
            "Cost sensitivity scenario used untouched holdout data"
        }
        val multiplier = row.optDouble("cost_multiplier", Double.NaN)
        val totalCosts = row.optDouble("total_estimated_costs", Double.NaN)
        val meanNet = row.optDouble("mean_net_return_pct", Double.NaN)
        val meanExcess = row.optDouble("mean_excess_return_pct", Double.NaN)
        val positiveFolds = row.optDouble("positive_fold_pct", Double.NaN)
        val drawdown = row.optDouble("maximum_fold_drawdown_pct", Double.NaN)
        val foldCount = row.optInt("fold_count", -1)
        val tradeCount = row.optInt("trade_count", -1)
        require(multiplier.isFinite() && multiplier >= 1.0) { "Cost multiplier is invalid" }
        require(totalCosts.isFinite() && totalCosts >= 0.0) { "Estimated costs are invalid" }
        require(meanNet.isFinite() && meanExcess.isFinite()) { "Cost-stress returns are non-finite" }
        require(positiveFolds.isFinite() && positiveFolds in 0.0..100.0) { "Positive-fold percentage is invalid" }
        require(drawdown.isFinite() && drawdown in 0.0..100.0) { "Maximum drawdown is invalid" }
        require(foldCount > 0 && tradeCount >= 0) { "Cost-stress sample counts are invalid" }
        ResearchCostScenario(
            multiplier = multiplier,
            foldCount = foldCount,
            tradeCount = tradeCount,
            totalEstimatedCosts = totalCosts,
            meanNetReturnPct = meanNet,
            meanExcessReturnPct = meanExcess,
            positiveFoldPct = positiveFolds,
            maximumFoldDrawdownPct = drawdown,
        )
    }
    require(scenarios.zipWithNext().all { (left, right) -> right.multiplier > left.multiplier }) {
        "Cost sensitivity multipliers must be strictly increasing"
    }
    require(scenarios.zipWithNext().all { (left, right) -> right.meanNetReturnPct <= left.meanNetReturnPct + 1e-9 }) {
        "Higher cost stress cannot improve net return"
    }
    return ResearchCostSensitivity(status, reason, scenarios)
}
