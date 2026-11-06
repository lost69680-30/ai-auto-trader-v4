package com.aiautotrader.v4

internal data class ResearchFoldEvidence(
    val tradeCount: Int,
    val totalCosts: Double?,
)

internal data class ResearchFoldSummary(
    val tradeCount: Int,
    val totalCosts: Double?,
)

internal fun summarizeResearchFolds(folds: Iterable<ResearchFoldEvidence>): ResearchFoldSummary {
    var trades = 0
    var costs = 0.0
    var hasCosts = false
    folds.forEach { fold ->
        require(fold.tradeCount >= 0) { "Validation trade count cannot be negative" }
        trades = Math.addExact(trades, fold.tradeCount)
        fold.totalCosts?.let { value ->
            require(value.isFinite() && value >= 0.0) { "Validation costs must be finite and non-negative" }
            costs += value
            require(costs.isFinite()) { "Validation cost total overflowed" }
            hasCosts = true
        }
    }
    return ResearchFoldSummary(tradeCount = trades, totalCosts = costs.takeIf { hasCosts })
}
