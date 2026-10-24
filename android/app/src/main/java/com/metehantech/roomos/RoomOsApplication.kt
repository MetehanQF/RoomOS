package com.metehantech.roomos

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.os.Build
import com.metehantech.roomos.auth.AndroidKeystoreCipher
import com.metehantech.roomos.auth.FileSessionStore
import com.metehantech.roomos.data.RoomOsRepository
import com.metehantech.roomos.network.RoomOsApiClient
import com.metehantech.roomos.telemetry.TelemetryPort
import com.metehantech.roomos.telemetry.TelemetryReporter
import com.metehantech.roomos.telemetry.TelemetryWorker
import com.google.firebase.messaging.FirebaseMessaging
import com.metehantech.roomos.push.ROOMOS_CRITICAL_CHANNEL
import com.metehantech.roomos.push.ROOMOS_INFO_CHANNEL
import com.metehantech.roomos.push.ROOMOS_WARNING_CHANNEL
import com.metehantech.roomos.push.RoomOsPush
import com.metehantech.roomos.push.PushReceiptWorker
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

class RoomOsApplication : Application() {
    private val applicationScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    val repository: RoomOsRepository by lazy {
        RoomOsRepository(
            RoomOsApiClient(),
            FileSessionStore(noBackupFilesDir, AndroidKeystoreCipher()),
            pairingName(Build.MODEL),
        )
    }

    /** Device telemetry (7D.3): the same app build serves the Phone, the A21 and the Tab. */
    val telemetryPort: TelemetryPort by lazy {
        val reporter by lazy { TelemetryReporter.create(this) }
        object : TelemetryPort {
            override suspend fun report(nextReportSeconds: Int) = reporter.report(nextReportSeconds)
            override fun scheduleBackground() = TelemetryWorker.schedule(this@RoomOsApplication)
            override fun cancelBackground() = TelemetryWorker.cancel(this@RoomOsApplication)
            override fun nowMillis() = System.currentTimeMillis()
        }
    }

    private val mutablePushEvents = MutableSharedFlow<RoomOsPush>(extraBufferCapacity = 8)

    /** 7G: pushes that arrived while RoomOS was on screen (shown in-app, not as a system notification). */
    val pushEvents: SharedFlow<RoomOsPush> = mutablePushEvents.asSharedFlow()

    override fun onCreate() {
        super.onCreate()
        createNotificationChannels()
    }

    fun onForegroundPush(push: RoomOsPush) {
        mutablePushEvents.tryEmit(push)
        push.messageId?.let { sendPushReceipt(it, "displayed") }
    }

    /** Durable, device-bound evidence with network constraints and three bounded attempts. */
    fun sendPushReceipt(messageId: String, event: String) {
        if (!repository.isPaired()) return
        runCatching { PushReceiptWorker.schedule(this, messageId, event) }
    }

    fun refreshFcmRegistration() {
        if (!repository.isPaired()) return
        FirebaseMessaging.getInstance().token.addOnCompleteListener { task ->
            val token = if (task.isSuccessful) task.result else null
            if (!token.isNullOrBlank()) registerFcmToken(token)
        }
    }

    fun registerFcmToken(token: String) {
        if (!repository.isPaired() || token.isBlank()) return
        applicationScope.launch {
            repeat(3) { attempt ->
                try {
                    repository.registerFcmToken(token)
                    return@launch
                } catch (_: Exception) {
                    if (attempt < 2) delay(1_000L shl attempt)
                }
            }
        }
    }

    private fun createNotificationChannels() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannels(
            listOf(
                NotificationChannel(
                    ROOMOS_CRITICAL_CHANNEL,
                    "RoomOS critical alerts",
                    NotificationManager.IMPORTANCE_HIGH,
                ).apply { lockscreenVisibility = android.app.Notification.VISIBILITY_PRIVATE },
                NotificationChannel(
                    ROOMOS_WARNING_CHANNEL,
                    "RoomOS warnings",
                    NotificationManager.IMPORTANCE_DEFAULT,
                ).apply { lockscreenVisibility = android.app.Notification.VISIBILITY_PRIVATE },
                NotificationChannel(
                    ROOMOS_INFO_CHANNEL,
                    "RoomOS information and recoveries",
                    NotificationManager.IMPORTANCE_LOW,
                ).apply { lockscreenVisibility = android.app.Notification.VISIBILITY_PRIVATE },
            ),
        )
    }
}

/** Pairing name from the device model code; the operator can rename the device afterwards. */
fun pairingName(model: String?): String {
    val code = model?.trim().orEmpty()
    val name = if (code.isNotEmpty()) "Android $code".take(48) else "Android device"
    return "$name Native"
}
