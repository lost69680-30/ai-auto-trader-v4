package com.aiautotrader.v4

internal enum class ResearchValidationBucket {
    PASSED,
    FAILED,
    INCOMPLETE,
}

internal data class ResearchValidationCounts(
    val passed: Int,
    val failed: Int,
    val incomplete: Int,
)

internal fun researchValidationBucket(status: String?): ResearchValidationBucket =
    when (status?.trim()?.uppercase()) {
        "PASSED" -> ResearchValidationBucket.PASSED
        "REJECTED", "FAILED", "DRIFT_FLAGGED", "DECAY_FLAGGED", "NOT_REPRODUCIBLE" ->
            ResearchValidationBucket.FAILED
        else -> ResearchValidationBucket.INCOMPLETE
    }

internal fun summarizeResearchValidationQuality(
    statuses: Iterable<String?>,
): ResearchValidationCounts {
    var passed = 0
    var failed = 0
    var incomplete = 0
    statuses.forEach { status ->
        when (researchValidationBucket(status)) {
            ResearchValidationBucket.PASSED -> passed++
            ResearchValidationBucket.FAILED -> failed++
            ResearchValidationBucket.INCOMPLETE -> incomplete++
        }
    }
    return ResearchValidationCounts(passed, failed, incomplete)
}
