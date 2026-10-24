package com.metehantech.roomos.network

import com.metehantech.roomos.data.AuthStatusDto
import kotlinx.coroutines.test.runTest
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class RoomOsApiClientTest {
    private lateinit var server: MockWebServer
    private lateinit var api: RoomOsApiClient

    @Before
    fun setup() {
        server = MockWebServer().also(MockWebServer::start)
        api = RoomOsApiClient(OkHttpClient(), origin = "http://127.0.0.1:${server.port}")
    }

    @After
    fun cleanup() = server.shutdown()

    @Test
    fun `native pairing sends no bearer and parses fixed scopes`() = runTest {
        server.enqueue(MockResponse().setBody("""{"authenticated":true,"session_token":"secret-native-token","expires_at":"2026-10-05T00:00:00Z","device":{"id":"device","friendly_name":"Phone","scopes":["lights:read","system:read"]}}""").setHeader("Content-Type", "application/json"))
        val paired = api.pair("ABCDEF123456", "Phone")
        val request = server.takeRequest()
        assertEquals("/api/v1/auth/native/pair", request.path)
        assertEquals("POST", request.method)
        assertEquals(null, request.getHeader("Authorization"))
        assertFalse(request.body.readUtf8().contains("session_token"))
        assertEquals("secret-native-token", paired.session_token)
    }

    @Test
    fun `read request uses bearer and rejects unauthorized`() = runTest {
        server.enqueue(MockResponse().setBody("""{"authenticated":true,"device":null}""").setHeader("Content-Type", "application/json"))
        api.get<AuthStatusDto>("/api/v1/auth/status", "native-token-123456")
        assertEquals("Bearer native-token-123456", server.takeRequest().getHeader("Authorization"))  // privacy-scan:allow fake test token

        server.enqueue(MockResponse().setResponseCode(401).setBody("{}"))
        val error = assertThrows(RoomOsApiException::class.java) {
            kotlinx.coroutines.runBlocking { api.get<AuthStatusDto>("/api/v1/auth/status", "expired-token") }
        }
        assertEquals(401, error.status)
    }

    @Test
    fun `FCM registration is bearer authenticated and never contains another device id`() = runTest {
        server.enqueue(
            MockResponse()
                .setHeader("Content-Type", "application/json")
                .setBody(
                    """{"provider":"fcm","device_id":"device-handset","destination_id":"fcm-device-handset","registered_at":"2026-10-05T00:00:00Z","token_fingerprint":"${"a".repeat(64)}","destination_enabled":false,"activated_at":"2026-10-05T00:00:00Z"}""",
                ),
        )
        val token = "fcm-registration:" + "x".repeat(80)
        val registered = api.registerFcmToken(token, "native-session")
        val request = server.takeRequest()
        assertEquals("/api/v1/native/notifications/fcm/register", request.path)
        assertEquals("POST", request.method)
        assertEquals("Bearer native-session", request.getHeader("Authorization"))
        val body = request.body.readUtf8()
        assertTrue(body.contains(token))
        assertFalse(body.contains("device_id"))
        assertFalse(registered.destination_enabled)
        assertFalse(registered.token_fingerprint.contains(token))
    }

    @Test
    fun `client only permits production origin or local test loopback`() {
        assertThrows(IllegalArgumentException::class.java) {
            RoomOsApiClient(origin = "https://home-assistant.local")
        }
    }

    @Test
    fun `native push settings and receipts are bound to bearer session`() = runTest {
        val settings = """{"device_id":"device-handset","display_name":"Phone","can_manage":true,"can_receive":true,"preferences":{"enabled":true},"channels":[],"recent":[],"capabilities":{"mode":"smart","delivery_active":true,"fcm_available":true,"timezone":"Europe/Istanbul"}}"""
        server.enqueue(MockResponse().setHeader("Content-Type", "application/json").setBody(settings))
        api.pushSettings("native-session")
        server.takeRequest().also {
            assertEquals("GET", it.method)
            assertEquals("/api/v1/native/push/settings", it.path)
            assertEquals("Bearer native-session", it.getHeader("Authorization"))
        }

        server.enqueue(MockResponse().setHeader("Content-Type", "application/json").setBody(settings))
        api.updatePushSettings(
            com.metehantech.roomos.data.PushPreferencesUpdateDto(warning = false),
            "native-session",
        )
        server.takeRequest().also {
            assertEquals("PUT", it.method)
            assertEquals("/api/v1/native/push/settings", it.path)
            assertTrue(it.body.readUtf8().contains("\"warning\":false"))
        }

        server.enqueue(MockResponse().setResponseCode(204))
        api.pushReceipt("psh_${"a".repeat(32)}", "opened", "native-session")
        server.takeRequest().also {
            assertEquals("POST", it.method)
            assertEquals("/api/v1/native/push/receipts", it.path)
            val body = it.body.readUtf8()
            assertTrue(body.contains("psh_${"a".repeat(32)}"))
            assertTrue(body.contains("opened"))
            assertFalse(body.contains("device_id"))
        }
    }

    @Test
    fun `native alarm writes use bearer and never send caller device identity`() = runTest {
        server.enqueue(MockResponse().setBody("""{"alert_id":"resource.workstation.disk_usage","until":"2026-10-17T18:00:00Z","severity":"warning"}"""))
        val snooze = api.snoozeAlert("resource.workstation.disk_usage", 8, "native-session")
        assertEquals("warning", snooze.severity)
        server.takeRequest().also {
            assertEquals("/api/v1/native/notifications/resource.workstation.disk_usage/snooze", it.path)
            assertEquals("Bearer native-session", it.getHeader("Authorization"))
            assertEquals(null, it.getHeader("Origin"))
            assertEquals("POST", it.method)
            assertEquals("""{"hours":8}""", it.body.readUtf8())
        }
        server.enqueue(MockResponse().setBody("""{"alert_id":"resource.workstation.disk_usage","cleared":true}"""))
        assertTrue(api.unsnoozeAlert("resource.workstation.disk_usage", "native-session").cleared)
        assertEquals("DELETE", server.takeRequest().method)
        server.enqueue(MockResponse().setBody("""{"device_id":"workstation","profile":"gaming","until":null}"""))
        api.setAlertProfile("workstation", "gaming", null, "native-session")
        server.takeRequest().also {
            assertEquals("/api/v1/native/alerts/profiles/workstation", it.path)
            assertEquals("PUT", it.method)
            assertFalse(it.body.readUtf8().contains("device_id"))
        }
    }
}
