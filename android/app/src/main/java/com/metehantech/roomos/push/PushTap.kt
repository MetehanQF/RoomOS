package com.metehantech.roomos.push

import java.net.URI

const val ROOMOS_PUSH_TAP_ACTION = "com.metehantech.roomos.action.OPEN_NOTIFICATION"
private const val TAP_SCHEME = "roomos"
private const val TAP_HOST = "notifications"

/** A unique PendingIntent identity for one accepted RoomOS message. */
fun pushTapUri(navigation: PushNavigation): String? {
    val validated = pushNavigation(navigation.alertId, navigation.messageId) ?: return null
    val messageId = validated.messageId ?: return null
    val alert = validated.alertId?.let { "?alert=$it" }.orEmpty()
    return "$TAP_SCHEME://$TAP_HOST/$messageId$alert"
}

/**
 * Parse the stable v9 action/data contract. Validated v8 extras are retained only
 * as an in-place-upgrade fallback for notifications that were already posted.
 */
fun parsePushTap(
    action: String?,
    data: String?,
    legacyAlertId: String?,
    legacyMessageId: String?,
): PushNavigation? {
    if (action == ROOMOS_PUSH_TAP_ACTION) {
        val uri = runCatching { URI(data ?: return null) }.getOrNull() ?: return null
        if (
            uri.scheme != TAP_SCHEME || uri.host != TAP_HOST || uri.fragment != null ||
            uri.userInfo != null || uri.port != -1
        ) return null
        val messageId = uri.path?.removePrefix("/")?.takeIf { !it.contains('/') } ?: return null
        val alertId = when {
            uri.rawQuery == null -> null
            uri.rawQuery.startsWith("alert=") && !uri.rawQuery.removePrefix("alert=").contains('&') ->
                uri.rawQuery.removePrefix("alert=")
            else -> return null
        }
        return pushNavigation(alertId, messageId)
    }
    if (data != null) return null
    return pushNavigation(legacyAlertId, legacyMessageId)
}
