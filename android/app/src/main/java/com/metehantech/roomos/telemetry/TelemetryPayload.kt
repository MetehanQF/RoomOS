package com.metehantech.roomos.telemetry

import java.time.Instant
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * One device measurement (Phase 7D.3). Every field is nullable: null means "Android could
 * not tell us right now" and RoomOS shows it as unknown — never as 0.
 */
data class TelemetryReadings(
    val batteryPercent: Double? = null,
    val charging: Boolean? = null,
    val powerSource: String? = null,
    val batteryTemperature: Double? = null,
    val batteryHealth: String? = null,
    val ramUsage: Double? = null,
    val ramTotalGb: Double? = null,
    val diskUsage: Double? = null,
    val storageFreeGb: Double? = null,
    val storageTotalGb: Double? = null,
    val networkType: String? = null,
    val networkConnected: Boolean? = null,
    val thermalStatus: String? = null,
    val androidVersion: String? = null,
    val deviceModel: String? = null,
)

object TelemetryPayload {
    const val SCHEMA_VERSION = 1

    /** Foreground cadence while RoomOS is open, and the WorkManager background cadence. */
    const val FOREGROUND_SECONDS = 60
    const val BACKGROUND_SECONDS = 900

    fun encode(readings: TelemetryReadings, sequence: Long, measuredAt: Instant, nextReportSeconds: Int): String {
        require(sequence > 0 && nextReportSeconds in 30..3600)
        return buildJsonObject {
            put("schema_version", SCHEMA_VERSION)
            put("sequence", sequence)
            put("measured_at", measuredAt.toString())
            put("next_report_seconds", nextReportSeconds)
            put("sensors", buildJsonObject {
                number("battery_percent", readings.batteryPercent)
                bool("charging", readings.charging)
                text("power_source", readings.powerSource)
                number("battery_temperature", readings.batteryTemperature)
                text("battery_health", readings.batteryHealth)
                number("ram_usage", readings.ramUsage)
                number("ram_total", readings.ramTotalGb)
                number("disk_usage", readings.diskUsage)
                number("storage_free", readings.storageFreeGb)
                number("storage_total", readings.storageTotalGb)
                text("network_type", readings.networkType)
                bool("network_connected", readings.networkConnected)
                text("thermal_status", readings.thermalStatus)
                text("android_version", readings.androidVersion)
                text("device_model", readings.deviceModel)
            })
        }.toString()
    }

    private fun kotlinx.serialization.json.JsonObjectBuilder.number(key: String, value: Double?) =
        put(key, value?.takeIf { it.isFinite() }?.let(::JsonPrimitive) ?: JsonNull)

    private fun kotlinx.serialization.json.JsonObjectBuilder.bool(key: String, value: Boolean?) =
        put(key, value?.let(::JsonPrimitive) ?: JsonNull)

    private fun kotlinx.serialization.json.JsonObjectBuilder.text(key: String, value: String?) =
        put(key, value?.let(::JsonPrimitive) ?: JsonNull)
}

/** Pure conversions from Android framework values (unit tested without a device). */
object TelemetryMapping {
    private const val GB = 1_000_000_000.0

    fun round1(value: Double): Double = Math.round(value * 10.0) / 10.0

    fun batteryPercent(level: Int, scale: Int): Double? =
        if (level < 0 || scale <= 0 || level > scale) null else round1(level * 100.0 / scale)

    /** EXTRA_TEMPERATURE is tenths of a degree Celsius; missing values are Int.MIN_VALUE or 0. */
    fun batteryTemperature(tenths: Int): Double? =
        if (tenths == Int.MIN_VALUE || tenths == 0 || tenths !in -200..800) null else round1(tenths / 10.0)

    /** BatteryManager.EXTRA_PLUGGED bit values (AC=1, USB=2, WIRELESS=4, DOCK=8). */
    fun powerSource(plugged: Int): String? = when {
        plugged < 0 -> null
        plugged == 0 -> "battery"
        plugged and 1 != 0 -> "ac"
        plugged and 2 != 0 -> "usb"
        plugged and 4 != 0 -> "wireless"
        plugged and 8 != 0 -> "dock"
        else -> null
    }

    /** BatteryManager.BATTERY_HEALTH_* — a status reported by the battery, not a capacity. */
    fun batteryHealth(health: Int): String? = when (health) {
        2 -> "good"
        3 -> "overheat"
        4 -> "dead"
        5 -> "over_voltage"
        6 -> "failure"
        7 -> "cold"
        else -> null // 1 = unknown
    }

    /** PowerManager.THERMAL_STATUS_* (API 29+). */
    fun thermalStatus(status: Int): String? = when (status) {
        0 -> "none"
        1 -> "light"
        2 -> "moderate"
        3 -> "severe"
        4 -> "critical"
        5 -> "emergency"
        6 -> "shutdown"
        else -> null
    }

    fun usagePercent(total: Long, available: Long): Double? =
        if (total <= 0 || available < 0 || available > total) null else round1((total - available) * 100.0 / total)

    fun gigabytes(bytes: Long): Double? = if (bytes < 0) null else round1(bytes / GB)

    fun androidVersion(release: String?): String? =
        release?.trim()?.takeIf { Regex("^[0-9]{1,2}(\\.[0-9]{1,3}){0,2}$").matches(it) }

    fun deviceModel(model: String?): String? =
        model?.trim()?.takeIf { Regex("^[A-Za-z0-9][A-Za-z0-9 ._()+-]{0,39}$").matches(it) }
}

/** Strictly increasing report sequence that survives app restarts and clock changes. */
class TelemetrySequence(private val load: () -> Long, private val store: (Long) -> Unit) {
    @Synchronized
    fun next(nowMillis: Long): Long {
        val value = maxOf(load() + 1, nowMillis)
        store(value)
        return value
    }
}
