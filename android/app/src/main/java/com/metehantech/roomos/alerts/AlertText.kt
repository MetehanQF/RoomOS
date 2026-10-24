package com.metehantech.roomos.alerts

import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/** 7I.2: server timestamps are UTC ISO-8601; the phone shows its own (Europe/Istanbul) wall clock. */
private val LOCAL = DateTimeFormatter.ofPattern("dd.MM HH:mm")

fun localTime(iso: String?, zone: ZoneId = ZoneId.systemDefault()): String =
    iso?.let { runCatching { OffsetDateTime.parse(it).atZoneSameInstant(zone).format(LOCAL) }.getOrNull() } ?: "—"

private val SOURCES = mapOf(
    "workstation" to "Workstation", "primary" to "Raspberry Pi 5", "secondary" to "Secondary", "dns" to "DNS",
    "frigate" to "Frigate", "nextcloud" to "Nextcloud", "home_assistant" to "Home Assistant",
)

fun sourceLabel(source: String): String = SOURCES[source] ?: source
