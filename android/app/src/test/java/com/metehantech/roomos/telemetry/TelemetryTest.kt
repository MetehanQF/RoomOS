package com.metehantech.roomos.telemetry

import com.metehantech.roomos.data.TelemetryState
import com.metehantech.roomos.data.TelemetryStatus
import com.metehantech.roomos.network.RoomOsApiException
import com.metehantech.roomos.pairingName
import java.time.Instant
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class TelemetryMappingTest {
    @Test
    fun `battery percent never invents zero`() {
        assertEquals(76.0, TelemetryMapping.batteryPercent(76, 100))
        assertEquals(50.0, TelemetryMapping.batteryPercent(128, 256))
        assertNull(TelemetryMapping.batteryPercent(-1, 100))
        assertNull(TelemetryMapping.batteryPercent(50, 0))
        assertNull(TelemetryMapping.batteryPercent(300, 100))
    }

    @Test
    fun `battery temperature is tenths of a degree and missing is null`() {
        assertEquals(31.4, TelemetryMapping.batteryTemperature(314))
        assertNull(TelemetryMapping.batteryTemperature(Int.MIN_VALUE))
        assertNull(TelemetryMapping.batteryTemperature(0))
        assertNull(TelemetryMapping.batteryTemperature(5000))
    }

    @Test
    fun `power source health and thermal codes`() {
        assertEquals("battery", TelemetryMapping.powerSource(0))
        assertEquals("ac", TelemetryMapping.powerSource(1))
        assertEquals("usb", TelemetryMapping.powerSource(2))
        assertEquals("wireless", TelemetryMapping.powerSource(4))
        assertEquals("dock", TelemetryMapping.powerSource(8))
        assertNull(TelemetryMapping.powerSource(-1))
        assertEquals("good", TelemetryMapping.batteryHealth(2))
        assertNull(TelemetryMapping.batteryHealth(1))
        assertEquals("none", TelemetryMapping.thermalStatus(0))
        assertEquals("severe", TelemetryMapping.thermalStatus(3))
        assertNull(TelemetryMapping.thermalStatus(42))
    }

    @Test
    fun `memory and storage usage`() {
        assertEquals(61.5, TelemetryMapping.usagePercent(8_000, 3_080))
        assertNull(TelemetryMapping.usagePercent(0, 0))
        assertNull(TelemetryMapping.usagePercent(100, 200))
        assertEquals(7.9, TelemetryMapping.gigabytes(7_912_345_678))
        assertNull(TelemetryMapping.gigabytes(-1))
    }

    @Test
    fun `identity texts are validated before leaving the phone`() {
        assertEquals("14", TelemetryMapping.androidVersion(" 14 "))
        assertEquals("8.1.0", TelemetryMapping.androidVersion("8.1.0"))
        assertNull(TelemetryMapping.androidVersion("Baklava"))
        assertEquals("Pixel 7", TelemetryMapping.deviceModel("Pixel 7"))
        assertNull(TelemetryMapping.deviceModel("<script>"))
    }

    @Test
    fun `pairing names come from the model code with one build`() {
        assertEquals("Android Pixel 7 Native", pairingName("Pixel 7"))
        assertEquals("Android Pixel Tablet Native", pairingName("Pixel Tablet"))
        assertEquals("Android Pixel 8 Native", pairingName("Pixel 8"))
        assertEquals("Android device Native", pairingName(null))
    }
}

class TelemetryPayloadTest {
    private val at = Instant.parse("2026-10-15T09:00:00Z")

    @Test
    fun `payload carries device time, cadence and explicit nulls`() {
        val body = TelemetryPayload.encode(
            TelemetryReadings(batteryPercent = 76.0, charging = true, networkType = "wifi", deviceModel = "SM-A725F"),
            sequence = 42, measuredAt = at, nextReportSeconds = 60,
        )
        val json = Json.parseToJsonElement(body).jsonObject
        assertEquals(setOf("schema_version", "sequence", "measured_at", "next_report_seconds", "sensors"), json.keys)
        assertEquals("2026-10-15T09:00:00Z", json["measured_at"]!!.jsonPrimitive.content)
        val sensors = json["sensors"]!!.jsonObject
        assertEquals(76.0, sensors["battery_percent"]!!.jsonPrimitive.content.toDouble(), 0.0)
        assertEquals(JsonNull, sensors["ram_usage"])
        assertEquals(15, sensors.size)
        assertTrue("cpu_usage" !in sensors && "device_id" !in json)
    }

    @Test(expected = IllegalArgumentException::class)
    fun `cadence outside the server contract is refused`() {
        TelemetryPayload.encode(TelemetryReadings(), 1, at, 5)
    }

    @Test
    fun `sequence strictly increases across restarts and clock jumps`() {
        var stored = 0L
        val sequence = TelemetrySequence({ stored }, { stored = it })
        assertEquals(1_000L, sequence.next(1_000))
        assertEquals(1_001L, sequence.next(500)) // clock moved back
        assertEquals(5_000L, sequence.next(5_000))
        val afterRestart = TelemetrySequence({ stored }, { stored = it })
        assertEquals(5_001L, afterRestart.next(5_000))
    }
}

class TelemetryReporterTest {
    private fun reporter(send: suspend (String) -> Unit) =
        TelemetryReporter({ TelemetryReadings(batteryPercent = 50.0) }, send, TelemetrySequence({ 0 }, {}), { Instant.parse("2026-10-15T09:00:00Z") })

    @Test
    fun `server answers map to outcomes`() = runTest {
        var sent: String? = null
        assertEquals(TelemetryOutcome.SENT, reporter { sent = it }.report(60))
        assertTrue(sent!!.contains("\"next_report_seconds\":60"))
        assertEquals(TelemetryOutcome.NOT_PERMITTED, reporter { throw RoomOsApiException(403, "x") }.report(60))
        assertEquals(TelemetryOutcome.AUTHORIZATION_REQUIRED, reporter { throw RoomOsApiException(401, "x") }.report(60))
        assertEquals(TelemetryOutcome.RATE_LIMITED, reporter { throw RoomOsApiException(429, "x") }.report(60))
        assertEquals(TelemetryOutcome.REJECTED, reporter { throw RoomOsApiException(422, "x") }.report(60))
        assertEquals(TelemetryOutcome.FAILED, reporter { throw java.io.IOException("offline") }.report(60))
    }
}

@OptIn(ExperimentalCoroutinesApi::class)
class TelemetryControllerTest {
    private class FakePort(var outcome: TelemetryOutcome = TelemetryOutcome.SENT, val clock: () -> Long = { 1_000L }) : TelemetryPort {
        val reports = mutableListOf<Int>()
        var scheduled = 0
        var cancelled = 0
        override suspend fun report(nextReportSeconds: Int): TelemetryOutcome = outcome.also { reports += nextReportSeconds }
        override fun scheduleBackground() { scheduled += 1 }
        override fun cancelBackground() { cancelled += 1 }
        override fun nowMillis() = clock()
    }

    private fun TestScope.controller(port: FakePort, statuses: MutableList<TelemetryStatus>) =
        TelemetryController(this, port, { statuses += it }, intervalMillis = 60_000)

    @Test
    fun `foreground reports once a minute and repeated syncs do not stack loops`() = runTest {
        val port = FakePort(clock = { testScheduler.currentTime })
        val statuses = mutableListOf<TelemetryStatus>()
        val controller = controller(port, statuses)
        controller.sync(permitted = true, foreground = true)
        controller.sync(permitted = true, foreground = true) // e.g. refresh after every WS event
        runCurrent()
        advanceTimeBy(150_000)
        assertEquals(listOf(60, 60, 60), port.reports)
        assertEquals(TelemetryState.ACTIVE, statuses.last().state)
        controller.background(permitted = true)
        runCurrent()
        assertEquals(900, port.reports.last())
        assertEquals(1, port.scheduled)
        advanceTimeBy(600_000)
        assertEquals(4, port.reports.size) // no foreground loop left running
    }

    @Test
    fun `no permission means nothing is measured or sent`() = runTest {
        val port = FakePort()
        val statuses = mutableListOf<TelemetryStatus>()
        val controller = controller(port, statuses)
        controller.sync(permitted = false, foreground = true)
        controller.background(permitted = false)
        advanceTimeBy(600_000)
        assertTrue(port.reports.isEmpty())
        assertEquals(0, port.scheduled)
        assertEquals(TelemetryState.NOT_ENABLED, statuses.last().state)
    }

    @Test
    fun `a 403 stops foreground and background reporting`() = runTest {
        val port = FakePort(TelemetryOutcome.NOT_PERMITTED)
        val statuses = mutableListOf<TelemetryStatus>()
        controller(port, statuses).sync(permitted = true, foreground = true)
        advanceTimeBy(300_000)
        assertEquals(1, port.reports.size)
        assertTrue(port.cancelled >= 2)
        assertEquals(TelemetryState.NOT_ENABLED, statuses.last().state)
    }

    @Test
    fun `network failures keep the loop and show a problem without replaying`() = runTest {
        val port = FakePort(TelemetryOutcome.FAILED)
        val statuses = mutableListOf<TelemetryStatus>()
        val controller = controller(port, statuses)
        controller.sync(permitted = true, foreground = true)
        runCurrent()
        assertEquals(TelemetryState.PROBLEM, statuses.last().state)
        port.outcome = TelemetryOutcome.SENT
        advanceTimeBy(61_000)
        assertEquals(TelemetryState.ACTIVE, statuses.last().state)
        assertEquals(listOf(60, 60), port.reports)
        controller.disable()
    }

    @Test
    fun `leaving right after a foreground report waits out the server rate limit`() = runTest {
        val port = FakePort(clock = { testScheduler.currentTime })
        val statuses = mutableListOf<TelemetryStatus>()
        val controller = controller(port, statuses)
        controller.sync(permitted = true, foreground = true)
        runCurrent() // foreground report at t=0
        advanceTimeBy(5_000)
        controller.background(permitted = true)
        runCurrent()
        assertEquals(listOf(60), port.reports) // not yet: would be a 429
        advanceTimeBy(11_001)
        assertEquals(listOf(60, 900), port.reports)
        assertEquals(1, port.scheduled)
    }

    @Test
    fun `returning during the wait cancels the pending background report`() = runTest {
        val port = FakePort(clock = { testScheduler.currentTime })
        val controller = controller(port, mutableListOf())
        controller.sync(permitted = true, foreground = true)
        runCurrent()
        advanceTimeBy(5_000)
        controller.background(permitted = true)
        advanceTimeBy(3_000)
        controller.sync(permitted = true, foreground = true)
        runCurrent()
        advanceTimeBy(20_000)
        assertEquals(listOf(60, 60), port.reports)
        assertEquals(0, port.scheduled)
        controller.disable()
    }

    @Test
    fun `background work starts after a delay instead of immediately`() {
        val source = java.io.File("src/main/java/com/metehantech/roomos/telemetry/TelemetryReporter.kt").readText()
        assertTrue(source.contains(".setInitialDelay(15, TimeUnit.MINUTES)"))
    }
}
