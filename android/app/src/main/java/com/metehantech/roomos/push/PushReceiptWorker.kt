package com.metehantech.roomos.push

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import com.metehantech.roomos.RoomOsApplication
import com.metehantech.roomos.data.AuthorizationRequired
import com.metehantech.roomos.network.RoomOsApiException
import java.util.concurrent.TimeUnit

enum class ReceiptDecision { SUCCESS, RETRY, DROP }

private const val MAX_RECEIPT_ATTEMPTS = 3
private val RECEIPT_EVENT = Regex("^(displayed|opened)$")

fun receiptWorkName(messageId: String, event: String): String {
    require(pushNavigation(null, messageId) != null)
    require(RECEIPT_EVENT.matches(event))
    return "roomos-push-receipt-$event-$messageId"
}

/** runAttemptCount is zero-based, so 0,1 retry and 2 is the final attempt. */
fun receiptDecision(runAttemptCount: Int, status: Int?): ReceiptDecision = when {
    status != null && status in 200..299 -> ReceiptDecision.SUCCESS
    status != null && status in 400..499 && status != 429 -> ReceiptDecision.DROP
    runAttemptCount + 1 >= MAX_RECEIPT_ATTEMPTS -> ReceiptDecision.DROP
    else -> ReceiptDecision.RETRY
}

class PushReceiptWorker(context: Context, parameters: WorkerParameters) : CoroutineWorker(context, parameters) {
    override suspend fun doWork(): Result {
        val messageId = inputData.getString(MESSAGE_ID) ?: return Result.failure()
        val event = inputData.getString(EVENT) ?: return Result.failure()
        if (runCatching { receiptWorkName(messageId, event) }.isFailure) return Result.failure()
        val app = applicationContext as RoomOsApplication
        if (!app.repository.isPaired()) return Result.failure()
        val decision = try {
            app.repository.pushReceipt(messageId, event)
            ReceiptDecision.SUCCESS
        } catch (_: AuthorizationRequired) {
            ReceiptDecision.DROP
        } catch (error: RoomOsApiException) {
            receiptDecision(runAttemptCount, error.status)
        } catch (_: Exception) {
            receiptDecision(runAttemptCount, null)
        }
        return when (decision) {
            ReceiptDecision.SUCCESS -> Result.success()
            ReceiptDecision.RETRY -> Result.retry()
            ReceiptDecision.DROP -> Result.failure()
        }
    }

    companion object {
        private const val MESSAGE_ID = "message_id"
        private const val EVENT = "event"

        fun schedule(context: Context, messageId: String, event: String) {
            val workName = receiptWorkName(messageId, event)
            val request = OneTimeWorkRequestBuilder<PushReceiptWorker>()
                .setInputData(workDataOf(MESSAGE_ID to messageId, EVENT to event))
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 10, TimeUnit.SECONDS)
                .build()
            WorkManager.getInstance(context.applicationContext)
                .enqueueUniqueWork(workName, ExistingWorkPolicy.KEEP, request)
        }
    }
}
