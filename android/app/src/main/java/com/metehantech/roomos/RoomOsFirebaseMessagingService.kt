package com.metehantech.roomos

import android.Manifest
import android.app.ActivityManager
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import com.metehantech.roomos.push.EXTRA_ALERT_ID
import com.metehantech.roomos.push.EXTRA_MESSAGE_ID
import com.metehantech.roomos.push.RoomOsPush
import com.metehantech.roomos.push.PushNavigation
import com.metehantech.roomos.push.ROOMOS_PUSH_TAP_ACTION
import com.metehantech.roomos.push.channelForSeverity
import com.metehantech.roomos.push.notificationPresentation
import com.metehantech.roomos.push.parsePush
import com.metehantech.roomos.push.pushTapUri

// Channel ids are a server contract: roomos_critical, roomos_warning, roomos_info (push/PushPayload.kt).
fun notificationChannelForSeverity(severity: String?): String = channelForSeverity(severity)

class RoomOsFirebaseMessagingService : FirebaseMessagingService() {
    override fun onNewToken(token: String) {
        // Token rotation: re-bind to this device's own RoomOS session; the server reads it live.
        (application as? RoomOsApplication)?.registerFcmToken(token)
    }

    override fun onMessageReceived(message: RemoteMessage) {
        val push = parsePush(message.data, message.notification?.title, message.notification?.body)
        val app = application as? RoomOsApplication
        if (appInForeground()) {
            // RoomOS is on screen: its live view refreshes; no system notification is raised.
            app?.onForegroundPush(push)
            return
        }
        if (
            android.os.Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) return
        if (!channelEnabled(push.channel)) return
        getSystemService(NotificationManager::class.java).notify(push.notificationId, buildRoomOsNotification(this, push))
        push.messageId?.let { app?.sendPushReceipt(it, "displayed") }
    }

    private fun channelEnabled(channel: String): Boolean {
        val manager = NotificationManagerCompat.from(this)
        if (!manager.areNotificationsEnabled()) return false
        if (android.os.Build.VERSION.SDK_INT < 26) return true
        return manager.getNotificationChannel(channel)?.importance != NotificationManager.IMPORTANCE_NONE
    }

    private fun appInForeground(): Boolean {
        val state = ActivityManager.RunningAppProcessInfo()
        ActivityManager.getMyMemoryState(state)
        return state.importance == ActivityManager.RunningAppProcessInfo.IMPORTANCE_FOREGROUND
    }
}

/** Kept outside the service so the exact posted content and tap contract are executable tests. */
internal fun buildRoomOsNotification(context: Context, push: RoomOsPush): android.app.Notification {
    val presentation = notificationPresentation(push)
    val intent = Intent(context, MainActivity::class.java).apply {
        flags = Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP
        action = ROOMOS_PUSH_TAP_ACTION
        pushTapUri(PushNavigation(push.alertId, push.messageId))?.let { data = Uri.parse(it) }
        push.alertId?.let { putExtra(EXTRA_ALERT_ID, it) }
        push.messageId?.let { putExtra(EXTRA_MESSAGE_ID, it) }
    }
    val pendingIntent = PendingIntent.getActivity(
        context,
        push.notificationId,
        intent,
        PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_CANCEL_CURRENT or PendingIntent.FLAG_ONE_SHOT,
    )
    val priority = when {
        push.urgent -> NotificationCompat.PRIORITY_HIGH
        push.severity == "info" || push.transition == "resolved" -> NotificationCompat.PRIORITY_LOW
        else -> NotificationCompat.PRIORITY_DEFAULT
    }
    // What the lock screen shows unless the user chose to reveal details there.
    val publicVersion = NotificationCompat.Builder(context, push.channel)
        .setSmallIcon(R.drawable.ic_roomos)
        .setContentTitle(presentation.publicTitle)
        .setContentText(presentation.publicBody)
        .build()
    return NotificationCompat.Builder(context, push.channel)
        .setSmallIcon(R.drawable.ic_roomos)
        .setContentTitle(presentation.title)
        .setContentText(presentation.body)
        .setStyle(NotificationCompat.BigTextStyle().bigText(presentation.body))
        .setContentIntent(pendingIntent)
        .setAutoCancel(true)
        .setPriority(priority)
        .setCategory(if (push.urgent) NotificationCompat.CATEGORY_ERROR else NotificationCompat.CATEGORY_STATUS)
        .setVisibility(
            if (push.lockScreenDetails) NotificationCompat.VISIBILITY_PUBLIC
            else NotificationCompat.VISIBILITY_PRIVATE,
        )
        .setPublicVersion(publicVersion)
        .setOnlyAlertOnce(!push.urgent)
        .build()
}
