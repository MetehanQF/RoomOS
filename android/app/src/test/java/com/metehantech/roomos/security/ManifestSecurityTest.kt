package com.metehantech.roomos.security

import java.io.File
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ManifestSecurityTest {
    @Test
    fun `manifest disables cleartext and backups and requests no broad permissions`() {
        val manifest = File("src/main/AndroidManifest.xml").readText()
        assertTrue(manifest.contains("android:usesCleartextTraffic=\"false\""))
        assertTrue(manifest.contains("android:allowBackup=\"false\""))
        assertTrue(manifest.contains("android.permission.INTERNET"))
        assertTrue(manifest.contains("android.permission.ACCESS_NETWORK_STATE"))
        assertTrue(manifest.contains("android.permission.POST_NOTIFICATIONS"))
        assertTrue(manifest.contains("RoomOsFirebaseMessagingService"))
        assertTrue(manifest.contains("android:exported=\"false\""))
        assertFalse(manifest.contains("CAMERA"))
        assertFalse(manifest.contains("LOCATION"))
        assertFalse(manifest.contains("WebView"))
    }

    @Test
    fun `notification channels match the server contract and token logging is absent`() {
        val source = File(
            "src/main/java/com/metehantech/roomos/RoomOsFirebaseMessagingService.kt",
        ).readText()
        val application = File(
            "src/main/java/com/metehantech/roomos/RoomOsApplication.kt",
        ).readText()
        assertTrue(source.contains("roomos_critical"))
        assertTrue(source.contains("roomos_warning"))
        assertTrue(source.contains("onNewToken"))
        assertFalse(source.contains("Log."))
        assertFalse(application.contains("Log."))
    }

    @Test
    fun `device telemetry reads no personal data and runs no foreground service`() {
        val manifest = File("src/main/AndroidManifest.xml").readText()
        assertTrue(manifest.contains("android.permission.FOREGROUND_SERVICE\" tools:node=\"remove\""))
        assertTrue(manifest.contains("SystemForegroundService\"\n            tools:node=\"remove\""))
        val telemetry = File("src/main/java/com/metehantech/roomos/telemetry").listFiles()!!
            .filter { it.extension == "kt" }.joinToString("\n") { it.readText() }
        for (forbidden in listOf(
            "LocationManager", "TelephonyManager", "ContactsContract", "Telephony.Sms", "ANDROID_ID",
            "getInstalledPackages", "WifiManager", "getSSID", "BluetoothAdapter", "Log.", "requestPermissions",
            "setForeground", "AlarmManager", "WakeLock",
        )) {
            assertFalse(forbidden, telemetry.contains(forbidden))
        }
    }

    @Test
    fun `vpn connectivity hint does not require android validated capability`() {
        val activity = File("src/main/java/com/metehantech/roomos/MainActivity.kt").readText()
        assertTrue(activity.contains("NetworkCapabilities.NET_CAPABILITY_INTERNET"))
        assertFalse(activity.contains("NetworkCapabilities.NET_CAPABILITY_VALIDATED"))
    }
}
