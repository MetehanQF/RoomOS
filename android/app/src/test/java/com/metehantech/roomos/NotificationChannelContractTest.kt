package com.metehantech.roomos

import com.metehantech.roomos.push.ROOMOS_CRITICAL_CHANNEL
import com.metehantech.roomos.push.ROOMOS_INFO_CHANNEL
import com.metehantech.roomos.push.ROOMOS_WARNING_CHANNEL
import org.junit.Assert.assertEquals
import org.junit.Test

class NotificationChannelContractTest {
    @Test
    fun `critical and warning severities use fixed RoomOS channels`() {
        assertEquals(ROOMOS_CRITICAL_CHANNEL, notificationChannelForSeverity("critical"))
        assertEquals(ROOMOS_WARNING_CHANNEL, notificationChannelForSeverity("warning"))
        assertEquals(ROOMOS_INFO_CHANNEL, notificationChannelForSeverity("info"))
        assertEquals(ROOMOS_WARNING_CHANNEL, notificationChannelForSeverity(null))
    }
}
