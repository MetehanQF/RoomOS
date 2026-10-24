package com.metehantech.roomos

import java.io.File
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class RoomOsViewModelContractTest {
    @Test
    fun `authoritative event refresh does not demote an open websocket`() {
        val source = File("src/main/java/com/metehantech/roomos/RoomOsViewModel.kt").readText()
        val refresh = source
            .substringAfter("private suspend fun authoritativeRefresh()")
            .substringBefore("private fun connectSocket()")

        assertFalse(refresh.contains("connection = ConnectionState.RECONNECTING"))
    }

    @Test
    fun `repeated android network capability callbacks are no-ops`() {
        val source = File("src/main/java/com/metehantech/roomos/RoomOsViewModel.kt").readText()
        val networkChanged = source
            .substringAfter("fun networkChanged(available: Boolean)")
            .substringBefore("private suspend fun authoritativeRefresh()")

        assertTrue(networkChanged.contains("if (!changed) return"))
    }

    @Test
    fun `repeated foreground callbacks do not create parallel connections`() {
        val source = File("src/main/java/com/metehantech/roomos/RoomOsViewModel.kt").readText()
        val foreground = source
            .substringAfter("fun foreground()")
            .substringBefore("private fun startConnection()")

        assertTrue(foreground.contains("compareAndSet(false, true)"))
        assertTrue(foreground.contains("return"))
    }
}
