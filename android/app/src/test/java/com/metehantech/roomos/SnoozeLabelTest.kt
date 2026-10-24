package com.metehantech.roomos

import org.junit.Assert.assertEquals
import org.junit.Test

class SnoozeLabelTest {
    @Test
    fun `offered snooze durations read as hours or whole days`() {
        assertEquals(listOf("2h", "8h", "24h", "7d"), listOf(2, 8, 24, 168).map(::snoozeLabel))
    }
}
