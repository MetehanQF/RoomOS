package com.metehantech.roomos.push

/**
 * Phase 7G push payload. Server messages are data-only and already minimal; this parser
 * re-validates every field so a malformed message can never open an arbitrary destination.
 * 7F (legacy) payloads without "v" are still accepted so the app works before 7G activation.
 */
data class RoomOsPush(
    val messageId: String?,
    val severity: String,
    val transition: String,
    val title: String,
    val body: String,
    val publicTitle: String,
    val publicBody: String,
    val alertId: String?,
    val tag: String,
    val lockScreenDetails: Boolean,
) {
    val channel: String get() = channelForSeverity(severity)
    val urgent: Boolean get() = severity == "critical" && transition != "resolved"
    val notificationId: Int get() = tag.hashCode()
}

const val ROOMOS_CRITICAL_CHANNEL = "roomos_critical"
const val ROOMOS_WARNING_CHANNEL = "roomos_warning"
const val ROOMOS_INFO_CHANNEL = "roomos_info"
const val EXTRA_ALERT_ID = "com.metehantech.roomos.extra.ALERT_ID"
const val EXTRA_MESSAGE_ID = "com.metehantech.roomos.extra.MESSAGE_ID"

private val MESSAGE_ID = Regex("^psh_[a-f0-9]{32}$")
private val ALERT_ID = Regex("^[a-z0-9._-]{1,80}$")
private val PATH = Regex("^/notifications(\\?alert=([a-z0-9._-]{1,80}))?$")
private const val PRIVATE_TITLE = "RoomOS — Yeni bildirim"
private const val FALLBACK_PUBLIC = "Ayrıntılar için RoomOS'u açın."

data class PushNavigation(val alertId: String?, val messageId: String?)

data class NotificationPresentation(
    val title: String,
    val body: String,
    val publicTitle: String,
    val publicBody: String,
)

/**
 * Enforce the RoomOS privacy preference in content we post, independently of the
 * device's own lock-screen policy. Android's publicVersion remains a second layer.
 */
fun notificationPresentation(push: RoomOsPush): NotificationPresentation = NotificationPresentation(
    title = if (push.lockScreenDetails) push.title else PRIVATE_TITLE,
    body = if (push.lockScreenDetails) push.body else FALLBACK_PUBLIC,
    publicTitle = PRIVATE_TITLE,
    publicBody = FALLBACK_PUBLIC,
)

/** Validate Intent extras independently of Android so forged intents cannot select another route. */
fun pushNavigation(alertId: String?, messageId: String?): PushNavigation? {
    if (alertId == null && messageId == null) return null
    if (alertId != null && !ALERT_ID.matches(alertId)) return null
    if (messageId != null && !MESSAGE_ID.matches(messageId)) return null
    return PushNavigation(alertId, messageId)
}

fun channelForSeverity(severity: String?): String = when (severity) {
    "critical" -> ROOMOS_CRITICAL_CHANNEL
    "info" -> ROOMOS_INFO_CHANNEL
    else -> ROOMOS_WARNING_CHANNEL
}

private fun text(value: String?, fallback: String, limit: Int): String =
    value?.trim()?.takeIf { it.isNotEmpty() }?.take(limit) ?: fallback

fun parsePush(data: Map<String, String>, notificationTitle: String? = null, notificationBody: String? = null): RoomOsPush {
    val severity = data["severity"]?.takeIf { it in setOf("critical", "warning", "info") } ?: "warning"
    val transition = data["transition"]?.takeIf { it in setOf("started", "escalated", "resolved", "summary") } ?: "started"
    val messageId = data["id"]?.takeIf(MESSAGE_ID::matches)
    val alertFromPath = data["path"]?.let(PATH::matchEntire)?.groupValues?.getOrNull(2)?.takeIf { it.isNotEmpty() }
    val alertId = alertFromPath
    // Versioned RoomOS messages are data-only. Never let an accidental FCM
    // notification block bypass the app-controlled privacy path.
    val legacy = data["v"] == null
    val title = text(data["title"] ?: notificationTitle.takeIf { legacy }, "RoomOS", 100)
    val body = text(data["body"] ?: notificationBody.takeIf { legacy }, FALLBACK_PUBLIC, 240)
    return RoomOsPush(
        messageId = messageId,
        severity = severity,
        transition = transition,
        title = title,
        body = body,
        publicTitle = text(data["public_title"], title, 100),
        publicBody = text(data["public_body"], FALLBACK_PUBLIC, 240),
        alertId = alertId,
        tag = alertId ?: messageId ?: data["notification_id"]?.take(80) ?: "roomos",
        // Legacy payloads never carried the preference: keep the 7F behaviour (details shown).
        lockScreenDetails = data["lock_screen_details"]?.let { it == "1" } ?: legacy,
    )
}
