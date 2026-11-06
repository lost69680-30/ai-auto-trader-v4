package com.aiautotrader.v4

import org.junit.Assert.assertEquals
import org.junit.Test

class ResearchValidationQualityTest {
    @Test
    fun onlyExplicitPassedValidationCountsAsPassed() {
        assertEquals(ResearchValidationBucket.PASSED, researchValidationBucket("PASSED"))
        assertEquals(ResearchValidationBucket.INCOMPLETE, researchValidationBucket("EVALUATED"))
    }

    @Test
    fun rejectedValidationCountsAsFailedEvenWhenExecutionWasEvaluated() {
        assertEquals(ResearchValidationBucket.FAILED, researchValidationBucket("REJECTED"))
    }

    @Test
    fun insufficientDataCountsAsIncompleteNotFailed() {
        assertEquals(ResearchValidationBucket.INCOMPLETE, researchValidationBucket("INSUFFICIENT_DATA"))
    }

    @Test
    fun missingOrUnknownQualityCountsAsIncomplete() {
        assertEquals(ResearchValidationBucket.INCOMPLETE, researchValidationBucket(null))
        assertEquals(ResearchValidationBucket.INCOMPLETE, researchValidationBucket("UNKNOWN"))
    }

    @Test
    fun summarySeparatesPassFailAndIncompleteQuality() {
        assertEquals(
            ResearchValidationCounts(passed = 1, failed = 2, incomplete = 3),
            summarizeResearchValidationQuality(
                listOf("PASSED", "REJECTED", "FAILED", "INSUFFICIENT_DATA", "EVALUATED", null)
            ),
        )
    }
}

    
    @Test
    fun validationFoldSummaryAggregatesOnlyFiniteNonNegativeEvidence() {
        assertEquals(
            ResearchFoldSummary(tradeCount = 12, totalCosts = 3.75),
            summarizeResearchFolds(
                listOf(
                    ResearchFoldEvidence(tradeCount = 5, totalCosts = 1.25),
                    ResearchFoldEvidence(tradeCount = 7, totalCosts = 2.50),
                )
            ),
        )
    }

    @Test
    fun emptyValidationFoldSummaryDoesNotInventEvidence() {
        assertEquals(ResearchFoldSummary(tradeCount = 0, totalCosts = null), summarizeResearchFolds(emptyList()))
    }

    @Test(expected = IllegalArgumentException::class)
    fun negativeValidationTradeCountIsRejected() {
        summarizeResearchFolds(listOf(ResearchFoldEvidence(tradeCount = -1, totalCosts = 0.0)))
    }

    @Test(expected = IllegalArgumentException::class)
    fun nonFiniteValidationCostsAreRejected() {
        summarizeResearchFolds(listOf(ResearchFoldEvidence(tradeCount = 1, totalCosts = Double.NaN)))
    }
