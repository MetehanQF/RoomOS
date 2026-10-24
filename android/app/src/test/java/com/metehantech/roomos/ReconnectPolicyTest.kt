package com.metehantech.roomos

import org.junit.Assert.assertEquals
import org.junit.Test

class ReconnectPolicyTest {
    @Test
    fun `uses bounded exponential backoff and bounded jitter`() {
        val policy = ReconnectPolicy()
        assertEquals(1_000, policy.delayMillis(0, 0))
        assertEquals(2_250, policy.delayMillis(1, 250))
        assertEquals(30_500, policy.delayMillis(99, 9_999))
    }
}
