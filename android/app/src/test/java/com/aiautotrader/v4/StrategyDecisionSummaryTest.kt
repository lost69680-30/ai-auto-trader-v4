package com.aiautotrader.v4

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class StrategyDecisionSummaryTest {
    @Test fun summarizesEachStrategyAndItsAdmissionReasons() {
        val selection = JSONObject(
            """{"strategy_assessments":[
                {"strategy_id":"day","decision":"NO_TRADE","orders_authorized":false,
                 "payload":{"reasons":["timeframes conflict","spread too wide"]}},
                {"strategy_id":"long_term","decision":"ANALYSIS_ONLY","orders_authorized":false,
                 "payload":{"reasons":["long-term context only"]}}
            ]}"""
        )

        assertEquals(
            listOf(
                "day · NO_TRADE — timeframes conflict; spread too wide",
                "long_term · ANALYSIS_ONLY — long-term context only",
            ),
            parseStrategyDecisionSummaries(selection),
        )
    }

    @Test fun rejectsStrategyRowsWithoutExplicitPaperOnlyAuthorization() {
        val selection = JSONObject(
            """{"strategy_assessments":[
                {"strategy_id":"day","decision":"NO_TRADE",
                 "payload":{"reasons":["not safe"]}}
            ]}"""
        )

        assertThrows(IllegalArgumentException::class.java) {
            parseStrategyDecisionSummaries(selection)
        }
    }

    @Test fun acceptsOlderReportsWithoutStrategyBreakdownAsEmpty() {
        assertEquals(emptyList<String>(), parseStrategyDecisionSummaries(JSONObject()))
    }
}
