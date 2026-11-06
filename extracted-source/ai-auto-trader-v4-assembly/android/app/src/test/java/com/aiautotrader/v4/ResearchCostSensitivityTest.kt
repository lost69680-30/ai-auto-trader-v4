package com.aiautotrader.v4

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ResearchCostSensitivityTest {
    private fun outcomes(status: String = "EVALUATED", useHoldout: Boolean = false, orders: Boolean = false): JSONObject {
        val scenarios = if (status == "UNAVAILABLE") "[]" else """
            [
              {"cost_multiplier":1.0,"fold_count":4,"trade_count":40,"total_estimated_costs":10.0,"mean_net_return_pct":1.2,"mean_excess_return_pct":0.8,"positive_fold_pct":75.0,"maximum_fold_drawdown_pct":2.0,"orders_authorized":false,"holdout_used":false},
              {"cost_multiplier":1.5,"fold_count":4,"trade_count":40,"total_estimated_costs":15.0,"mean_net_return_pct":0.9,"mean_excess_return_pct":0.5,"positive_fold_pct":50.0,"maximum_fold_drawdown_pct":2.5,"orders_authorized":false,"holdout_used":false},
              {"cost_multiplier":2.0,"fold_count":4,"trade_count":40,"total_estimated_costs":20.0,"mean_net_return_pct":0.5,"mean_excess_return_pct":0.1,"positive_fold_pct":25.0,"maximum_fold_drawdown_pct":3.0,"orders_authorized":false,"holdout_used":false}
            ]
        """.trimIndent()
        return JSONObject().put("cost_sensitivity", JSONObject()
            .put("status", status)
            .put("reason", if (status == "UNAVAILABLE") "Trade ledger is missing" else JSONObject.NULL)
            .put("orders_authorized", orders)
            .put("holdout_used", useHoldout)
            .put("scenarios", org.json.JSONArray(scenarios)))
    }

    @Test
    fun parsesEvaluatedScenariosInIncreasingCostOrder() {
        val report = parseResearchCostSensitivity(outcomes())
        assertEquals("EVALUATED", report.status)
        assertEquals(listOf(1.0, 1.5, 2.0), report.scenarios.map { it.multiplier })
        assertEquals(40, report.scenarios[0].tradeCount)
    }

    @Test
    fun marksMissingLegacyReportAsMissing() {
        val report = parseResearchCostSensitivity(JSONObject())
        assertEquals("MISSING", report.status)
        assertTrue(report.scenarios.isEmpty())
    }

    @Test
    fun parsesUnavailableWithoutInventingScenarios() {
        val report = parseResearchCostSensitivity(outcomes(status = "UNAVAILABLE"))
        assertEquals("UNAVAILABLE", report.status)
        assertTrue(report.scenarios.isEmpty())
    }

    @Test(expected = IllegalArgumentException::class)
    fun rejectsReportsThatUseUntouchedHoldout() {
        parseResearchCostSensitivity(outcomes(useHoldout = true))
    }

    @Test(expected = IllegalArgumentException::class)
    fun rejectsReportsThatAuthorizeOrders() {
        parseResearchCostSensitivity(outcomes(orders = true))
    }

    @Test(expected = IllegalArgumentException::class)
    fun rejectsCostStressWhereHigherCostsImproveNetReturn() {
        val bad = outcomes()
        val scenarios = bad.getJSONObject("cost_sensitivity").getJSONArray("scenarios")
        scenarios.getJSONObject(2).put("mean_net_return_pct", 2.0)
        parseResearchCostSensitivity(bad)
    }

    @Test(expected = IllegalArgumentException::class)
    fun rejectsMissingPerScenarioSafetyFlags() {
        val bad = outcomes()
        bad.getJSONObject("cost_sensitivity").getJSONArray("scenarios")
            .getJSONObject(0).remove("orders_authorized")
        parseResearchCostSensitivity(bad)
    }
}
