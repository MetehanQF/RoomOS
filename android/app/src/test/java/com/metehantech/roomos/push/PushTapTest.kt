package com.metehantech.roomos.push

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Test

class PushTapTest {
    private val first = "psh_${"1".repeat(32)}"
    private val second = "psh_${"2".repeat(32)}"

    @Test
    fun `canary tap has a stable route to notification center without an alert`() {
        val uri = pushTapUri(PushNavigation(alertId = null, messageId = first))

        assertEquals("roomos://notifications/$first", uri)
        assertEquals(
            PushNavigation(alertId = null, messageId = first),
            parsePushTap(ROOMOS_PUSH_TAP_ACTION, uri, null, null),
        )
    }

    @Test
    fun `real alert tap preserves distinct alert and message ids`() {
        val navigation = PushNavigation("resource.workstation.disk_usage", first)
        val uri = pushTapUri(navigation)

        assertEquals(
            PushNavigation("resource.workstation.disk_usage", first),
            parsePushTap(ROOMOS_PUSH_TAP_ACTION, uri, null, null),
        )
    }

    @Test
    fun `cold and warm lifecycle use the same validated tap contract`() {
        val uri = pushTapUri(PushNavigation(null, first))
        val coldStart = parsePushTap(ROOMOS_PUSH_TAP_ACTION, uri, null, null)
        val warmOnNewIntent = parsePushTap(ROOMOS_PUSH_TAP_ACTION, uri, null, null)

        assertEquals(PushNavigation(null, first), coldStart)
        assertEquals(coldStart, warmOnNewIntent)
    }

    @Test
    fun `v8 extras remain valid after in-place v9 update`() {
        assertEquals(
            PushNavigation("alert-1", first),
            parsePushTap(action = null, data = null, legacyAlertId = "alert-1", legacyMessageId = first),
        )
    }

    @Test
    fun `launcher and malformed external routes cannot produce an opened receipt`() {
        assertNull(parsePushTap("android.intent.action.MAIN", null, null, null))
        assertNull(parsePushTap(ROOMOS_PUSH_TAP_ACTION, "https://evil.example/$first", null, null))
        assertNull(parsePushTap(ROOMOS_PUSH_TAP_ACTION, "roomos://notifications/not-a-message", null, null))
        assertNull(parsePushTap(ROOMOS_PUSH_TAP_ACTION, "roomos://notifications/$first?alert=../../token", null, null))
    }

    @Test
    fun `message uri is unique even when alerts share a collapse key`() {
        val alert = "resource.workstation.disk_usage"
        assertNotEquals(
            pushTapUri(PushNavigation(alert, first)),
            pushTapUri(PushNavigation(alert, second)),
        )
    }
}
