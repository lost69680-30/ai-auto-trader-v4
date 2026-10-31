package com.aiautotrader.v4

import java.time.Duration
import java.time.Instant
import java.time.OffsetDateTime

private const val DEFAULT_MARKET_OBSERVATION_MAX_AGE_SECONDS = 90.0

/**
 * Parse an exchange/backend observation timestamp and reject data that is
 * missing, malformed, stale, or more than one second ahead of the local clock.
 */
internal fun parseFreshMarketObservation(
    observedAt: String?,
    now: Instant = Instant.now(),
    maxAgeSeconds: Double = DEFAULT_MARKET_OBSERVATION_MAX_AGE_SECONDS,
): Instant? {
    if (observedAt.isNullOrBlank() || !maxAgeSeconds.isFinite() || maxAgeSeconds <= 0.0) {
        return null
    }
    val observedInstant = try {
        OffsetDateTime.parse(observedAt.trim()).toInstant()
    } catch (_: Exception) {
        return null
    }
    if (observedInstant.isAfter(now.plusSeconds(1))) return null
    val ageSeconds = Duration.between(observedInstant, now).toMillis() / 1000.0
    if (ageSeconds > maxAgeSeconds) return null
    return observedInstant
}

internal enum class MarketDisplayFreshness { LIVE, RECENT_DISCONNECTED, STALE, UNAVAILABLE }

/**
 * A retained snapshot is never labelled live merely because it remains on screen.
 * A live label requires a connected WebSocket and a fresh observation timestamp.
 */
internal fun classifyMarketDisplayFreshness(
    observedAt: String?,
    websocketStatus: String,
    now: Instant = Instant.now(),
    maxAgeSeconds: Double = DEFAULT_MARKET_OBSERVATION_MAX_AGE_SECONDS,
): MarketDisplayFreshness {
    val observed = parseFreshMarketObservation(observedAt, now, maxAgeSeconds)
        ?: return if (observedAt.isNullOrBlank()) MarketDisplayFreshness.UNAVAILABLE else MarketDisplayFreshness.STALE
    val connected = websocketStatus.trim().uppercase(java.util.Locale.ROOT) == "CONNECTED"
    return if (connected) MarketDisplayFreshness.LIVE else MarketDisplayFreshness.RECENT_DISCONNECTED
}
