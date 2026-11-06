package com.aiautotrader.v4

import org.junit.Assert.assertEquals
import org.junit.Test

class ResearchQualityCountsTest {
    private fun item(
        qualityStatus: String,
        holdoutUsedForSelection: Boolean = false,
        ordersAuthorized: Boolean = false,
        foldCount: Int = 4,
        validationTradeCount: Int = 40,
        meanExcessReturnPct: Double? = 1.25,
        positiveFoldPct: Double? = 75.0,
        maximumFoldDrawdownPct: Double? = 10.0,
        failureReasons: List<String> = emptyList(),
    ) = StrategyResearchExperimentItem(
        experimentId = "exp-$qualityStatus",
        symbol = "BTCUSDT",
        candidateFamily = "trend_sma_cross",
        status = "EVALUATED",
        qualityStatus = qualityStatus,
        datasetStart = "2026-11-01T00:00:00Z",
        datasetEnd = "2026-11-03T00:00:00Z",
        foldCount = foldCount,
        validationTradeCount = validationTradeCount,
        meanExcessReturnPct = meanExcessReturnPct,
        positiveFoldPct = positiveFoldPct,
        maximumFoldDrawdownPct = maximumFoldDrawdownPct,
        qualityFailureReasons = failureReasons,
        holdoutUsedForSelection = holdoutUsedForSelection,
        holdoutNetPnl = null,
        holdoutTradeCount = 0,
        ordersAuthorized = ordersAuthorized,
    )

    @Test fun countsValidationQualityInsteadOfEvaluationCompletion() {
        val counts = calculateResearchQualityCounts(
            listOf(
                item("EVALUATED"),
                item("REJECTED"),
                item("INSUFFICIENT_DATA"),
                item("INCOMPLETE"),
            )
        )

        assertEquals(1, counts.qualified)
        assertEquals(1, counts.rejected)
        assertEquals(2, counts.insufficient)
    }

    @Test fun completedButWeakOrUnsafeEvidenceCannotCountAsQualified() {
        val counts = calculateResearchQualityCounts(
            listOf(
                item("EVALUATED", holdoutUsedForSelection = true),
                item("EVALUATED", ordersAuthorized = true),
                item("EVALUATED", foldCount = 3),
                item("EVALUATED", validationTradeCount = 20),
                item("EVALUATED", meanExcessReturnPct = -0.1),
                item("EVALUATED", positiveFoldPct = 25.0),
                item("EVALUATED", maximumFoldDrawdownPct = 30.0),
                item("EVALUATED", failureReasons = listOf("validation failure")),
            )
        )

        assertEquals(0, counts.qualified)
        assertEquals(0, counts.rejected)
        assertEquals(8, counts.insufficient)
    }
}
