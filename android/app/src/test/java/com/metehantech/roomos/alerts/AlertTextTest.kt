package com.metehantech.roomos.alerts

import java.time.ZoneId
import org.junit.Assert.assertEquals
import org.junit.Test

class AlertTextTest {
    private val istanbul = ZoneId.of("Europe/Istanbul")

    @Test
    fun `server UTC timestamps render in Istanbul wall-clock time`() {
        assertEquals("16.10 21:21", localTime("2026-10-16T18:21:58.017028Z", istanbul))
        assertEquals("17.10 02:30", localTime("2026-10-16T23:30:00+00:00", istanbul))
    }

    @Test
    fun `missing or malformed timestamps never crash`() {
        assertEquals("—", localTime(null, istanbul))
        assertEquals("—", localTime("yesterday", istanbul))
    }

    @Test
    fun `sources use friendly names and keep unknown ids`() {
        assertEquals("Workstation", sourceLabel("workstation"))
        assertEquals("future_box", sourceLabel("future_box"))
    }
}
