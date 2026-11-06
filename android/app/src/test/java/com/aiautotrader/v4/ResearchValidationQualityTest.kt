package com.aiautotrader.v4

import org.junit.Assert.assertEquals
import org.junit.Test

class ResearchValidationQualityTest {
    @Test
    fun acceptedValidationCountsAsPassed() {
        assertEquals(ResearchValidationBucket.PASSED, researchValidationBucket("PASSED"))
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
            ResearchValidationCounts(passed = 1, failed = 2, incomplete = 2),
            summarizeResearchValidationQuality(
                listOf("PASSED", "REJECTED", "FAILED", "INSUFFICIENT_DATA", null)
            ),
        )
    }
}
