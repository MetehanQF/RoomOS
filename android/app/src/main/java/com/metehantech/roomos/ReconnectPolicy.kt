package com.metehantech.roomos

import kotlin.math.min

class ReconnectPolicy(
    private val maximumDelayMillis: Long = 30_000,
    private val jitterMillis: Long = 500,
) {
    fun delayMillis(attempt: Int, jitter: Long): Long {
        val exponential = 1_000L * (1L shl min(attempt.coerceAtLeast(0), 5))
        return min(maximumDelayMillis, exponential) + jitter.coerceIn(0, jitterMillis)
    }
}
