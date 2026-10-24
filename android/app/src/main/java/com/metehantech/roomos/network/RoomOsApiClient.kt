package com.metehantech.roomos.network

import com.metehantech.roomos.data.NativePairingDto
import com.metehantech.roomos.data.PairingRequestDto
import com.metehantech.roomos.data.FcmRegistrationDto
import com.metehantech.roomos.data.FcmRegistrationRequestDto
import com.metehantech.roomos.data.PushPreferencesUpdateDto
import com.metehantech.roomos.data.PushReceiptDto
import com.metehantech.roomos.data.PushSettingsDto
import com.metehantech.roomos.data.AlertProfileResultDto
import com.metehantech.roomos.data.AlertSnoozeResultDto
import com.metehantech.roomos.data.AlertUnsnoozeResultDto
import java.io.IOException
import java.util.concurrent.atomic.AtomicReference
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener

class RoomOsApiException(val status: Int, message: String) : IOException(message)

class RoomOsApiClient(
    private val http: OkHttpClient = OkHttpClient(),
    @PublishedApi internal val json: Json = Json { ignoreUnknownKeys = true },
    private val origin: String = PRODUCTION_ORIGIN,
) {
    companion object {
        const val PRODUCTION_ORIGIN = "https://roomos.example.com"
    }

    private val activeSocket = AtomicReference<WebSocket?>(null)

    init {
        require(origin == PRODUCTION_ORIGIN || origin.startsWith("http://127.0.0.1:"))
    }

    suspend inline fun <reified T> get(path: String, token: String): T =
        decode(execute(path, token))

    suspend fun pair(code: String, friendlyName: String): NativePairingDto {
        val body = json.encodeToString(PairingRequestDto(code, friendlyName))
            .toRequestBody("application/json".toMediaType())
        return decode(execute("/api/v1/auth/native/pair", null, body))
    }

    suspend fun registerFcmToken(token: String, sessionToken: String): FcmRegistrationDto {
        val body = json.encodeToString(FcmRegistrationRequestDto(token))
            .toRequestBody("application/json".toMediaType())
        return decode(
            execute(
                "/api/v1/native/notifications/fcm/register",
                sessionToken,
                body,
            ),
        )
    }

    /** Phase 7D.3: this paired app's own device telemetry. The server derives the device. */
    suspend fun sendTelemetry(payload: String, sessionToken: String) {
        execute("/api/v1/native/devices/telemetry", sessionToken, payload.toRequestBody("application/json".toMediaType()))
    }

    /** Phase 7G: this device's own push preferences (server derives the device from the session). */
    suspend fun pushSettings(sessionToken: String): PushSettingsDto =
        decode(execute("/api/v1/native/push/settings", sessionToken))

    suspend fun updatePushSettings(update: PushPreferencesUpdateDto, sessionToken: String): PushSettingsDto {
        val body = json.encodeToString(update).toRequestBody("application/json".toMediaType())
        return decode(execute("/api/v1/native/push/settings", sessionToken, body, method = "PUT"))
    }

    suspend fun pushReceipt(messageId: String, event: String, sessionToken: String) {
        val body = json.encodeToString(PushReceiptDto(messageId, event)).toRequestBody("application/json".toMediaType())
        execute("/api/v1/native/push/receipts", sessionToken, body)
    }

    private fun validatedAlertId(value: String): String {
        require(Regex("[a-z0-9._-]{1,80}").matches(value))
        return value
    }

    private fun profileDevice(value: String): String {
        require(value in setOf("workstation", "primary", "secondary"))
        return value
    }

    suspend fun snoozeAlert(alertId: String, hours: Int, sessionToken: String): AlertSnoozeResultDto {
        require(hours in setOf(2, 8, 24, 168))
        val body = "{\"hours\":$hours}".toRequestBody("application/json".toMediaType())
        return decode(execute("/api/v1/native/notifications/${validatedAlertId(alertId)}/snooze", sessionToken, body))
    }

    suspend fun unsnoozeAlert(alertId: String, sessionToken: String): AlertUnsnoozeResultDto =
        decode(execute("/api/v1/native/notifications/${validatedAlertId(alertId)}/snooze", sessionToken, method = "DELETE"))

    suspend fun setAlertProfile(deviceId: String, profile: String, hours: Int?, sessionToken: String): AlertProfileResultDto {
        require(profile in setOf("normal", "gaming", "development"))
        require(hours == null || hours in 1..24)
        val duration = hours?.let { ",\"hours\":$it" }.orEmpty()
        val body = "{\"profile\":\"$profile\"$duration}".toRequestBody("application/json".toMediaType())
        return decode(execute("/api/v1/native/alerts/profiles/${profileDevice(deviceId)}", sessionToken, body, method = "PUT"))
    }

    suspend fun execute(
        path: String,
        token: String?,
        body: okhttp3.RequestBody? = null,
        method: String = if (body == null) "GET" else "POST",
    ): String =
        withContext(Dispatchers.IO) {
            require(path.startsWith("/api/v1/") && !path.contains(".."))
            require(method in setOf("GET", "POST", "PUT", "DELETE"))
            val builder = Request.Builder().url(origin + path)
                .header("Accept", "application/json")
            if (token != null) builder.header("Authorization", "Bearer $token")
            builder.method(method, body)
            http.newCall(builder.build()).execute().use(::read)
        }

    inline fun <reified T> decode(payload: String): T = json.decodeFromString(payload)

    private fun read(response: Response): String {
        val content = response.body?.string().orEmpty()
        if (!response.isSuccessful) throw RoomOsApiException(response.code, "RoomOS request failed")
        return content
    }

    fun openEvents(token: String, listener: WebSocketListener): WebSocket {
        closeEvents()
        val request = Request.Builder()
            .url(origin.replaceFirst("https://", "wss://") + "/api/v1/native/events")
            .header("Authorization", "Bearer $token")
            .build()
        return http.newWebSocket(request, listener).also(activeSocket::set)
    }

    fun closeEvents() {
        activeSocket.getAndSet(null)?.close(1000, "lifecycle")
    }

    fun activeSocketCount(): Int = if (activeSocket.get() == null) 0 else 1
}
