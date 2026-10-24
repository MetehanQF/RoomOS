package com.metehantech.roomos

import android.app.Notification
import android.app.PendingIntent
import android.content.ComponentName
import android.content.pm.ActivityInfo
import androidx.test.core.app.ApplicationProvider
import com.metehantech.roomos.push.EXTRA_ALERT_ID
import com.metehantech.roomos.push.EXTRA_MESSAGE_ID
import com.metehantech.roomos.push.ROOMOS_PUSH_TAP_ACTION
import com.metehantech.roomos.push.parsePush
import com.metehantech.roomos.push.parsePushTap
import com.metehantech.roomos.push.PushNavigation
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.GraphicsMode
import org.robolectric.annotation.Config
import org.robolectric.annotation.ConscryptMode

@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.LEGACY)
@ConscryptMode(ConscryptMode.Mode.OFF)
@Config(sdk = [28])
class PushNotificationContractTest {
    private val messageId = "psh_${"8".repeat(32)}"

    @Test
    fun `details-off redacts the actual notification object on every device policy`() {
        val push = parsePush(
            mapOf(
                "v" to "1",
                "id" to messageId,
                "path" to "/notifications?alert=system.disk-workstation",
                "title" to "RoomOS — Uyarı",
                "body" to "Workstation C sürücüsü yüzde 87 dolu.",
                "public_title" to "Workstation C sürücüsü",
                "public_body" to "Yüzde 87 dolu.",
                "lock_screen_details" to "0",
            ),
        )

        val notification = buildRoomOsNotification(ApplicationProvider.getApplicationContext(), push)
        assertEquals("RoomOS — Yeni bildirim", notification.extras.getCharSequence(Notification.EXTRA_TITLE))
        assertEquals("Ayrıntılar için RoomOS'u açın.", notification.extras.getCharSequence(Notification.EXTRA_TEXT))
        assertFalse(notification.extras.toString().contains("yüzde 87"))
    }

    @Test
    fun `pending intent carries a stable canary route and one-shot semantics`() {
        val push = parsePush(
            mapOf(
                "v" to "1",
                "id" to messageId,
                "path" to "/notifications",
                "tag" to messageId,
                "title" to "RoomOS — Test",
                "body" to "Test",
            ),
        )

        val notification = buildRoomOsNotification(ApplicationProvider.getApplicationContext(), push)
        val pending = notification.contentIntent
        assertNotNull(pending)
        val intent = shadowOf(pending!!).savedIntent
        assertEquals(ROOMOS_PUSH_TAP_ACTION, intent.action)
        assertEquals("roomos://notifications/$messageId", intent.dataString)
        assertEquals(messageId, intent.getStringExtra(EXTRA_MESSAGE_ID))
        assertFalse(intent.hasExtra(EXTRA_ALERT_ID))
        intent.removeExtra(EXTRA_MESSAGE_ID)
        assertEquals(
            PushNavigation(null, messageId),
            parsePushTap(intent.action, intent.dataString, null, null),
        )
        assertEquals(
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_CANCEL_CURRENT or PendingIntent.FLAG_ONE_SHOT,
            shadowOf(pending).flags,
        )
    }

    @Test
    fun `launcher activity uses single-top for warm taps and the same intent handles cold starts`() {
        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        val info = context.packageManager.getActivityInfo(ComponentName(context, MainActivity::class.java), 0)
        assertEquals(ActivityInfo.LAUNCH_SINGLE_TOP, info.launchMode)
    }
}
