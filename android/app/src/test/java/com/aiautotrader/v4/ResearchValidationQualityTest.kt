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
