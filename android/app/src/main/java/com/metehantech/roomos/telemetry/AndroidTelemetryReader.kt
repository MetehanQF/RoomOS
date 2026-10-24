package com.metehantech.roomos.telemetry

import android.app.ActivityManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.BatteryManager
import android.os.Build
import android.os.Environment
import android.os.PowerManager
import android.os.StatFs
import androidx.core.content.ContextCompat

/**
 * Reads the device with public framework APIs only. No runtime permission is requested:
 * ACCESS_NETWORK_STATE is a normal (install-time) permission the app already declares.
 * No location, contacts, SMS, phone state, package list or identifiers are read. CPU load
 * and CPU temperature are not readable by apps since Android 8 and are never reported.
 */
class AndroidTelemetryReader(private val context: Context) {
    fun read(): TelemetryReadings {
        val battery = ContextCompat.registerReceiver(
            context, null, IntentFilter(Intent.ACTION_BATTERY_CHANGED), ContextCompat.RECEIVER_NOT_EXPORTED,
        )
        val memory = runCatching {
            ActivityManager.MemoryInfo().also {
                context.getSystemService(ActivityManager::class.java).getMemoryInfo(it)
            }
        }.getOrNull()
        val storage = runCatching { StatFs(Environment.getDataDirectory().path) }.getOrNull()
        val (networkType, networkConnected) = network()
        return TelemetryReadings(
            batteryPercent = battery?.let {
                TelemetryMapping.batteryPercent(
                    it.getIntExtra(BatteryManager.EXTRA_LEVEL, -1),
                    it.getIntExtra(BatteryManager.EXTRA_SCALE, -1),
                )
            },
            charging = battery?.getIntExtra(BatteryManager.EXTRA_STATUS, -1)?.let { status ->
                when (status) {
                    BatteryManager.BATTERY_STATUS_CHARGING -> true
                    BatteryManager.BATTERY_STATUS_DISCHARGING,
                    BatteryManager.BATTERY_STATUS_NOT_CHARGING,
                    BatteryManager.BATTERY_STATUS_FULL -> false
                    else -> null
                }
            },
            powerSource = battery?.let { TelemetryMapping.powerSource(it.getIntExtra(BatteryManager.EXTRA_PLUGGED, -1)) },
            batteryTemperature = battery?.let {
                TelemetryMapping.batteryTemperature(it.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, Int.MIN_VALUE))
            },
            batteryHealth = battery?.let { TelemetryMapping.batteryHealth(it.getIntExtra(BatteryManager.EXTRA_HEALTH, -1)) },
            ramUsage = memory?.let { TelemetryMapping.usagePercent(it.totalMem, it.availMem) },
            ramTotalGb = memory?.let { TelemetryMapping.gigabytes(it.totalMem) },
            diskUsage = storage?.let { TelemetryMapping.usagePercent(it.totalBytes, it.availableBytes) },
            storageFreeGb = storage?.let { TelemetryMapping.gigabytes(it.availableBytes) },
            storageTotalGb = storage?.let { TelemetryMapping.gigabytes(it.totalBytes) },
            networkType = networkType,
            networkConnected = networkConnected,
            thermalStatus = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                TelemetryMapping.thermalStatus(context.getSystemService(PowerManager::class.java).currentThermalStatus)
            } else {
                null
            },
            androidVersion = TelemetryMapping.androidVersion(Build.VERSION.RELEASE),
            deviceModel = TelemetryMapping.deviceModel(Build.MODEL),
        )
    }

    /**
     * Physical transport of the network actually carrying traffic. A VPN (e.g. Tailscale) is
     * looked through to its underlying Wi-Fi/cellular network. "Connected" means Android
     * validated internet access, not merely that RoomOS is reachable.
     */
    @Suppress("DEPRECATION") // allNetworks: the only way to see a VPN's underlying network on API 26-30
    private fun network(): Pair<String?, Boolean?> {
        val connectivity = context.getSystemService(ConnectivityManager::class.java) ?: return null to null
        val active = connectivity.activeNetwork ?: return "none" to false
        val caps = connectivity.getNetworkCapabilities(active) ?: return null to null
        val physical = if (caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) {
            connectivity.allNetworks.asSequence()
                .mapNotNull(connectivity::getNetworkCapabilities)
                .firstOrNull { !it.hasTransport(NetworkCapabilities.TRANSPORT_VPN) && it.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) }
        } else {
            caps
        }
        val type = when {
            physical == null -> "vpn"
            physical.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "wifi"
            physical.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "cellular"
            physical.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) -> "ethernet"
            physical.hasTransport(NetworkCapabilities.TRANSPORT_BLUETOOTH) -> "bluetooth"
            else -> "other"
        }
        val validated = (physical ?: caps).hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)
        return type to validated
    }
}
