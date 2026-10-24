package com.metehantech.roomos.telemetry

import com.metehantech.roomos.data.TelemetryState
import com.metehantech.roomos.data.TelemetryStatus
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/** Side effects the controller needs; the app wires them to Android, tests to fakes. */
interface TelemetryPort {
    suspend fun report(nextReportSeconds: Int): TelemetryOutcome
    fun scheduleBackground()
    fun cancelBackground()
    fun nowMillis(): Long
}

/**
 * Foreground: one report per minute while RoomOS is visible (a single coroutine, no
 * busy loop). Leaving the foreground sends one report announcing the ~15-minute
 * background cadence and hands over to WorkManager. Permission is decided by the server:
 * without telemetry:write:self nothing runs, and a 403 stops everything.
 */
class TelemetryController(
    private val scope: CoroutineScope,
    private val port: TelemetryPort,
    private val onStatus: (TelemetryStatus) -> Unit,
    private val intervalMillis: Long = TelemetryPayload.FOREGROUND_SECONDS * 1000L,
    private val minGapMillis: Long = MIN_GAP_MILLIS,
) {
    companion object {
        /** The server accepts one report per 15 s per app; leave a margin. */
        const val MIN_GAP_MILLIS = 16_000L
    }

    private var job: Job? = null
    private var leaving: Job? = null // pending "going to background" report
    private var status = TelemetryStatus()

    /** Called after every authoritative refresh (scopes may have changed, e.g. after 4409). */
    fun sync(permitted: Boolean, foreground: Boolean) {
        if (!permitted) {
            disable()
            return
        }
        if (!foreground || job?.isActive == true) return
        leaving?.cancel() // back in the foreground before the background report went out
        port.cancelBackground() // the app is open: the foreground loop reports instead
        job = scope.launch {
            while (isActive) {
                if (!handle(port.report(TelemetryPayload.FOREGROUND_SECONDS))) break
                delay(intervalMillis)
            }
        }
    }

    fun background(permitted: Boolean) {
        job?.cancel()
        job = null
        if (!permitted) return
        leaving = scope.launch {
            // Leaving right after a foreground report would be rate limited (429) and the
            // phone would turn Unknown until WorkManager's next run: wait out the gap.
            val sinceLast = status.lastSentAtMillis?.let { port.nowMillis() - it }
            if (sinceLast != null && sinceLast in 0 until minGapMillis) delay(minGapMillis - sinceLast)
            if (handle(port.report(TelemetryPayload.BACKGROUND_SECONDS))) port.scheduleBackground()
        }
    }

    /** Unpaired, revoked or scope removed: stop foreground and background reporting. */
    fun disable() {
        job?.cancel()
        job = null
        leaving?.cancel()
        port.cancelBackground()
        publish(TelemetryStatus(TelemetryState.NOT_ENABLED, status.lastSentAtMillis))
    }

    /** Returns false when reporting must stop. */
    private fun handle(outcome: TelemetryOutcome): Boolean = when (outcome) {
        TelemetryOutcome.SENT -> {
            publish(TelemetryStatus(TelemetryState.ACTIVE, port.nowMillis()))
            true
        }
        TelemetryOutcome.RATE_LIMITED -> true // a report was accepted moments ago
        TelemetryOutcome.FAILED, TelemetryOutcome.REJECTED -> {
            publish(status.copy(state = TelemetryState.PROBLEM))
            true // network loss: the next cycle measures again, nothing is replayed
        }
        TelemetryOutcome.NOT_PERMITTED, TelemetryOutcome.AUTHORIZATION_REQUIRED -> {
            port.cancelBackground()
            publish(TelemetryStatus(TelemetryState.NOT_ENABLED, status.lastSentAtMillis))
            false
        }
    }

    private fun publish(next: TelemetryStatus) {
        status = next
        onStatus(next)
    }
}
