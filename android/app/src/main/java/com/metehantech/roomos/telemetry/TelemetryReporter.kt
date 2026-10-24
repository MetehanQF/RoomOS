package com.metehantech.roomos.telemetry

import android.content.Context
import androidx.core.content.edit
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.metehantech.roomos.RoomOsApplication
import com.metehantech.roomos.data.AuthorizationRequired
import com.metehantech.roomos.network.RoomOsApiException
import java.time.Instant
import java.util.concurrent.TimeUnit

enum class TelemetryOutcome { SENT, NOT_PERMITTED, RATE_LIMITED, REJECTED, FAILED, AUTHORIZATION_REQUIRED }

/**
 * Measures once and sends one report. Nothing is queued or backfilled: a report that cannot
 * be sent is dropped and the next one carries a fresh measurement, so RoomOS never shows an
 * old reading as new.
 */
class TelemetryReporter(
    private val read: () -> TelemetryReadings,
    private val send: suspend (String) -> Unit,
    private val sequence: TelemetrySequence,
    private val now: () -> Instant = Instant::now,
) {
    suspend fun report(nextReportSeconds: Int): TelemetryOutcome {
        val measuredAt = now()
        val body = TelemetryPayload.encode(read(), sequence.next(measuredAt.toEpochMilli()), measuredAt, nextReportSeconds)
        return try {
            send(body)
            TelemetryOutcome.SENT
        } catch (_: AuthorizationRequired) {
            TelemetryOutcome.AUTHORIZATION_REQUIRED
        } catch (error: RoomOsApiException) {
            when (error.status) {
                401 -> TelemetryOutcome.AUTHORIZATION_REQUIRED
                403 -> TelemetryOutcome.NOT_PERMITTED // scope removed or app not bound by the operator
                429 -> TelemetryOutcome.RATE_LIMITED
                else -> TelemetryOutcome.REJECTED
            }
        } catch (_: Exception) {
            TelemetryOutcome.FAILED
        }
    }

    companion object {
        private const val PREFERENCES = "roomos_telemetry"
        private const val SEQUENCE = "sequence"

        fun create(context: Context): TelemetryReporter {
            val app = context.applicationContext as RoomOsApplication
            val preferences = context.applicationContext.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)
            return TelemetryReporter(
                read = AndroidTelemetryReader(context.applicationContext)::read,
                send = app.repository::sendTelemetry,
                sequence = TelemetrySequence(
                    load = { preferences.getLong(SEQUENCE, 0L) },
                    store = { value -> preferences.edit { putLong(SEQUENCE, value) } },
                ),
            )
        }
    }
}

/**
 * Background reports while RoomOS is closed: WorkManager's 15-minute minimum, only with a
 * network, deferred freely by Doze/App Standby. No foreground service, no wake locks, no
 * exact alarms: when Android postpones the job, RoomOS shows the phone as Unknown instead
 * of pretending it is online.
 */
class TelemetryWorker(context: Context, parameters: WorkerParameters) : CoroutineWorker(context, parameters) {
    override suspend fun doWork(): Result {
        val app = applicationContext as RoomOsApplication
        if (!app.repository.isPaired()) {
            cancel(applicationContext)
            return Result.success()
        }
        return when (TelemetryReporter.create(applicationContext).report(TelemetryPayload.BACKGROUND_SECONDS)) {
            TelemetryOutcome.NOT_PERMITTED, TelemetryOutcome.AUTHORIZATION_REQUIRED -> {
                cancel(applicationContext) // re-enabled when the app sees the scope again
                Result.success()
            }
            else -> Result.success() // never retry with an old measurement
        }
    }

    companion object {
        private const val WORK = "roomos-device-telemetry"

        fun schedule(context: Context) {
            // The app just sent a report on leaving the foreground: without an initial delay
            // WorkManager runs a new periodic job at once and the server answers 429.
            val request = PeriodicWorkRequestBuilder<TelemetryWorker>(15, TimeUnit.MINUTES)
                .setInitialDelay(15, TimeUnit.MINUTES)
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .build()
            WorkManager.getInstance(context).enqueueUniquePeriodicWork(WORK, ExistingPeriodicWorkPolicy.KEEP, request)
        }

        fun cancel(context: Context) {
            WorkManager.getInstance(context).cancelUniqueWork(WORK)
        }
    }
}
