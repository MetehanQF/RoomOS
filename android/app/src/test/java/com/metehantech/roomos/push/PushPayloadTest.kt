package com.metehantech.roomos.push

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class PushPayloadTest {
    @Test
    fun `validated payload opens only a notification-center alert`() {
        val push = parsePush(
            mapOf(
                "v" to "1",
                "id" to "psh_${"a".repeat(32)}",
                "severity" to "critical",
                "transition" to "started",
                "path" to "/notifications?alert=system.disk-workstation",
                "tag" to "ignored-tag",
                "title" to "RoomOS — Kritik",
                "body" to "Workstation disk alanı kritik.",
                "public_title" to "RoomOS bildirimi",
                "public_body" to "Ayrıntılar için RoomOS'u açın.",
                "lock_screen_details" to "0",
            ),
        )

        assertEquals("system.disk-workstation", push.alertId)
        assertEquals("psh_${"a".repeat(32)}", push.messageId)
        assertEquals(ROOMOS_CRITICAL_CHANNEL, push.channel)
        assertTrue(push.urgent)
        assertFalse(push.lockScreenDetails)
    }

    @Test
    fun `canary message tag never becomes an alert id`() {
        val messageId = "psh_${"b".repeat(32)}"
        val push = parsePush(
            mapOf(
                "v" to "1",
                "id" to messageId,
                "path" to "/notifications",
                "tag" to messageId,
                "title" to "RoomOS — Test",
                "body" to "Synthetic canary details",
                "public_title" to "RoomOS — Test",
                "public_body" to "Ayrıntılar için RoomOS'u açın.",
                "lock_screen_details" to "0",
            ),
        )

        assertNull(push.alertId)
        assertEquals(messageId, push.messageId)
        assertEquals(messageId, push.tag)
        assertEquals(PushNavigation(null, messageId), pushNavigation(push.alertId, push.messageId))
    }

    @Test
    fun `details-off makes the primary notification content generic`() {
        val push = parsePush(
            mapOf(
                "v" to "1",
                "id" to "psh_${"c".repeat(32)}",
                "path" to "/notifications?alert=system.disk-workstation",
                "title" to "RoomOS — Uyarı",
                "body" to "Workstation C sürücüsü yüzde 87 dolu.",
                "public_title" to "Workstation C sürücüsü",
                "public_body" to "Yüzde 87 dolu.",
                "lock_screen_details" to "0",
            ),
        )

        val presentation = notificationPresentation(push)
        assertEquals("RoomOS — Yeni bildirim", presentation.title)
        assertEquals("Ayrıntılar için RoomOS'u açın.", presentation.body)
        assertEquals("RoomOS — Yeni bildirim", presentation.publicTitle)
        assertEquals("Ayrıntılar için RoomOS'u açın.", presentation.publicBody)
    }

    @Test
    fun `details-on keeps detailed primary content but a generic public version`() {
        val push = parsePush(
            mapOf(
                "v" to "1",
                "id" to "psh_${"d".repeat(32)}",
                "title" to "RoomOS — Kritik",
                "body" to "Home Assistant bağlantısı kesildi.",
                "public_title" to "Unexpected sensitive public title",
                "public_body" to "Unexpected sensitive public body",
                "lock_screen_details" to "1",
            ),
        )

        val presentation = notificationPresentation(push)
        assertEquals("RoomOS — Kritik", presentation.title)
        assertEquals("Home Assistant bağlantısı kesildi.", presentation.body)
        assertEquals("RoomOS — Yeni bildirim", presentation.publicTitle)
        assertEquals("Ayrıntılar için RoomOS'u açın.", presentation.publicBody)
    }

    @Test
    fun `versioned data message ignores an unexpected notification payload`() {
        val push = parsePush(
            mapOf(
                "v" to "1",
                "id" to "psh_${"e".repeat(32)}",
            ),
            notificationTitle = "Untrusted display title",
            notificationBody = "Untrusted display body",
        )

        assertEquals("RoomOS", push.title)
        assertEquals("Ayrıntılar için RoomOS'u açın.", push.body)
    }

    @Test
    fun `untrusted destination and identifiers are discarded`() {
        val push = parsePush(
            mapOf(
                "id" to "../../token",
                "path" to "https://evil.example/steal",
                "tag" to "bad/tag",
                "severity" to "root",
                "title" to " ",
            ),
        )

        assertNull(push.alertId)
        assertNull(push.messageId)
        assertEquals("warning", push.severity)
        assertEquals("RoomOS", push.title)
    }

    @Test
    fun `server text is bounded and legacy payload preserves lock-screen behavior`() {
        val push = parsePush(mapOf("title" to "T".repeat(200), "body" to "B".repeat(500)))

        assertEquals(100, push.title.length)
        assertEquals(240, push.body.length)
        assertTrue(push.lockScreenDetails)
    }

    @Test
    fun `push navigation accepts only validated ids`() {
        assertEquals(
            PushNavigation("alert-1", "psh_${"f".repeat(32)}"),
            pushNavigation("alert-1", "psh_${"f".repeat(32)}"),
        )
        assertNull(pushNavigation("../../settings", "psh_${"f".repeat(32)}"))
        assertNull(pushNavigation(null, "not-a-message"))
        assertNull(pushNavigation(null, null))
    }
}
