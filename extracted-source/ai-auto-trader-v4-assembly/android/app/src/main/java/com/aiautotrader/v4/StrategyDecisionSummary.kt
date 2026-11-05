package com.aiautotrader.v4

import org.json.JSONArray
import org.json.JSONObject

/**
 * Produces a readable, paper-only summary of each strategy assessment.
 * Every source row must explicitly deny order authorization.
 */
internal fun parseStrategyDecisionSummaries(selection: JSONObject): List<String> {
    val assessments = selection.optJSONArray("strategy_assessments") ?: JSONArray()
    require(assessments.length() <= 20) { "Too many strategy assessments" }
    return (0 until assessments.length()).map { index ->
        val assessment = assessments.optJSONObject(index)
            ?: throw IllegalArgumentException("Malformed strategy assessment")
        require(!assessment.optBoolean("orders_authorized", true)) {
            "Strategy assessment did not explicitly deny order authorization"
        }
        val strategy = assessment.optString("strategy_id").trim()
        val decision = assessment.optString("decision").trim()
        require(strategy.isNotBlank() && decision.isNotBlank()) {
            "Strategy assessment is missing its identity or decision"
        }
        val payload = assessment.optJSONObject("payload")
            ?: throw IllegalArgumentException("Strategy assessment payload is missing")
        val reasons = payload.optJSONArray("reasons") ?: JSONArray()
        val reasonText = (0 until reasons.length())
            .mapNotNull { index -> reasons.optString(index).trim().takeIf { it.isNotBlank() } }
            .distinct()
        "$strategy · $decision" + if (reasonText.isEmpty()) "" else " — " + reasonText.joinToString("; ")
    }
}
