package com.metehantech.roomos.push

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class PushReceiptPolicyTest {
    private val message = "psh_${"a".repeat(32)}"

    @Test
    fun `message and event form a stable duplicate-suppression key`() {
        assertEquals(
            "roomos-push-receipt-opened-$message",
            receiptWorkName(message, "opened"),
        )
        assertEquals(receiptWorkName(message, "opened"), receiptWorkName(message, "opened"))
        assertNotEquals(receiptWorkName(message, "opened"), receiptWorkName(message, "displayed"))
        assertThrows(IllegalArgumentException::class.java) { receiptWorkName("not-a-message", "opened") }
        assertThrows(IllegalArgumentException::class.java) { receiptWorkName(message, "received") }
    }

    @Test
    fun `offline rate-limit and server failures retry at most three total attempts`() {
        assertEquals(ReceiptDecision.RETRY, receiptDecision(runAttemptCount = 0, status = null))
        assertEquals(ReceiptDecision.RETRY, receiptDecision(runAttemptCount = 1, status = 429))
        assertEquals(ReceiptDecision.RETRY, receiptDecision(runAttemptCount = 1, status = 503))
        assertEquals(ReceiptDecision.DROP, receiptDecision(runAttemptCount = 2, status = null))
        assertEquals(ReceiptDecision.DROP, receiptDecision(runAttemptCount = 2, status = 500))
    }

    @Test
    fun `success auth and permanent client failures never retry`() {
        assertEquals(ReceiptDecision.SUCCESS, receiptDecision(runAttemptCount = 0, status = 204))
        assertEquals(ReceiptDecision.DROP, receiptDecision(runAttemptCount = 0, status = 401))
        assertEquals(ReceiptDecision.DROP, receiptDecision(runAttemptCount = 0, status = 403))
        assertEquals(ReceiptDecision.DROP, receiptDecision(runAttemptCount = 0, status = 404))
    }
}
