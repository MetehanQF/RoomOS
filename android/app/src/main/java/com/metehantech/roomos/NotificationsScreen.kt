package com.metehantech.roomos

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.metehantech.roomos.alerts.localTime
import com.metehantech.roomos.alerts.sourceLabel
import com.metehantech.roomos.data.NotificationItemDto
import com.metehantech.roomos.data.AlertInsightDto
import com.metehantech.roomos.data.AlertSnoozeDto
import com.metehantech.roomos.data.AlertProfilesDto
import com.metehantech.roomos.data.PushPreferencesUpdateDto
import com.metehantech.roomos.data.PushSettingsDto
import com.metehantech.roomos.data.RoomOsUiState

private val Muted = Color(0xFFBDB4CE)
private val CardColor = Color(0xCC171222)

fun severityColor(severity: String): Color = when (severity) {
    "critical" -> Color(0xFFEF5B5B)
    "warning" -> Color(0xFFE6A23C)
    else -> Color(0xFF4D9DE0)
}

/** Truthful labels: "accepted" means the push service took it, not that the phone showed it. */
fun deliveryLabel(status: String): String = when (status) {
    "accepted" -> "Push service accepted"
    "displayed" -> "Shown on device"
    "opened" -> "Opened"
    "pending" -> "Queued"
    "deferred" -> "Waiting for quiet hours to end"
    "skipped" -> "Not sent (preferences)"
    "aggregated" -> "Grouped into a summary"
    "superseded" -> "No longer relevant"
    "invalid" -> "Registration invalid"
    else -> status.replaceFirstChar(Char::uppercase)
}

@Composable
fun NotificationsScreen(
    state: RoomOsUiState,
    onBack: () -> Unit,
    onRefresh: () -> Unit,
    onUpdate: (PushPreferencesUpdateDto) -> Unit,
    onSnooze: (String, Int) -> Unit,
    onUnsnooze: (String) -> Unit,
    onProfile: (String, String, Int?) -> Unit,
) {
    BackHandler(onBack = onBack)
    Column(
        modifier = Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            TextButton(onClick = onBack, modifier = Modifier.heightIn(min = 48.dp)) { Text("‹ Back") }
            Text("Notifications", fontSize = 28.sp, fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
            TextButton(onClick = onRefresh, modifier = Modifier.heightIn(min = 48.dp)) { Text("Refresh") }
        }
        state.notificationsError?.let { Text(it, color = Color(0xFFFFB4B4)) }
        state.pushSettings?.let { PushSettingsCard(it, state.pushBusy, onUpdate) }
            ?: Text("Reading notification settings…", color = Muted)
        val catalog = state.notifications
        if (catalog != null) {
            Text("${catalog.active_count} active · ${catalog.unread_count} unread", color = Muted)
            if (catalog.notifications.isEmpty()) Text("No notifications yet.", color = Muted)
            catalog.notifications.forEach {
                NotificationCard(it, catalog.insights[it.id], catalog.snoozes[it.id],
                    focused = it.id == state.focusAlertId, canSnooze = state.canSnoozeAlerts,
                    busy = state.alarmBusy, onSnooze = onSnooze, onUnsnooze = onUnsnooze)
            }
        }
        state.alertProfiles?.let { ProfileCard(it, state.canEditProfiles, state.alarmBusy, onProfile) }
    }
}

@Composable
private fun NotificationCard(
    item: NotificationItemDto, insight: AlertInsightDto?, snooze: AlertSnoozeDto?, focused: Boolean,
    canSnooze: Boolean, busy: Boolean, onSnooze: (String, Int) -> Unit, onUnsnooze: (String) -> Unit,
) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(20.dp),
        colors = CardDefaults.cardColors(containerColor = CardColor),
        border = if (focused) BorderStroke(2.dp, Color(0xFFB08CFF)) else null,
    ) {
        Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Text(item.severity.replaceFirstChar(Char::uppercase), color = severityColor(item.severity), fontWeight = FontWeight.Bold, fontSize = 12.sp)
                Text(if (item.status == "active") "Active" else "Resolved", color = Muted, fontSize = 12.sp)
            }
            Text(item.title, fontSize = 17.sp, fontWeight = FontWeight.SemiBold)
            Text(item.message, color = Muted, fontSize = 13.sp)
            Text("${sourceLabel(item.source)} · since ${localTime(item.started_at)}", color = Muted, fontSize = 12.sp)
            insight?.let {
                val value = it.value?.let { number -> "${number}${it.unit.orEmpty()}" } ?: "not available"
                Text("Current: $value", color = Muted, fontSize = 13.sp)
                it.reason?.let { reason -> Text("Reason: $reason", color = Muted, fontSize = 12.sp) }
            }
            if (item.status == "active") {
                Text(snooze?.let { "Alarm snoozed until ${localTime(it.until)}" }
                    ?: "Alarm notifications are not snoozed", color = Muted, fontSize = 12.sp)
                if (!canSnooze) Text("View only · alerts:snooze permission required", color = Muted, fontSize = 12.sp)
                if (snooze != null) {
                    OutlinedButton(onClick = { onUnsnooze(item.id) }, enabled = canSnooze && !busy,
                        modifier = Modifier.heightIn(min = 48.dp)) { Text("Remove alarm snooze") }
                } else {
                    Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                        listOf(listOf(2, 8), listOf(24, 168)).forEach { row ->
                            Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                                row.forEach { hours ->
                                    OutlinedButton(onClick = { onSnooze(item.id, hours) }, enabled = canSnooze && !busy,
                                        modifier = Modifier.heightIn(min = 48.dp)) { Text("Snooze ${snoozeLabel(hours)}") }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

internal fun snoozeLabel(hours: Int): String = if (hours % 24 == 0 && hours >= 48) "${hours / 24}d" else "${hours}h"

private val profileTargets = listOf("workstation" to "Workstation", "primary" to "Raspberry Pi 5", "secondary" to "Secondary")

@Composable
private fun ProfileCard(profiles: AlertProfilesDto, canEdit: Boolean, busy: Boolean, onProfile: (String, String, Int?) -> Unit) {
    var duration by remember { mutableIntStateOf(4) }
    Card(shape = RoundedCornerShape(20.dp), colors = CardDefaults.cardColors(containerColor = CardColor)) {
        Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text("RESOURCE USAGE PROFILES", color = Color(0xFFA98CE6), fontWeight = FontWeight.Bold, fontSize = 12.sp)
            Text("CPU, GPU and RAM warn only after 6 hours of unbroken high usage. Gaming and Development pause them; temperature, disk and service alarms stay active.", color = Muted, fontSize = 12.sp)
            if (!canEdit) Text("View only · alerts:profile permission required", color = Muted, fontSize = 12.sp)
            Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                listOf(listOf(1, 4), listOf(8, 24)).forEach { row ->
                    Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        row.forEach { hours ->
                            OutlinedButton(onClick = { duration = hours }, modifier = Modifier.heightIn(min = 48.dp)) {
                                Text(if (duration == hours) "✓ ${hours}h" else "${hours}h")
                            }
                        }
                    }
                }
            }
            profileTargets.forEach { (id, label) ->
                val value = profiles.devices[id]
                Text("$label · ${value?.profile ?: "normal"}${value?.until?.let { " until ${localTime(it)}" }.orEmpty()}", fontWeight = FontWeight.SemiBold)
                Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                    listOf(listOf("normal" to "Normal", "gaming" to "Gaming"), listOf("development" to "Development")).forEach { row ->
                        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                            row.forEach { (profile, title) ->
                                OutlinedButton(onClick = { onProfile(id, profile, if (profile == "normal") null else duration) },
                                    enabled = canEdit && !busy && value?.profile != profile,
                                    modifier = Modifier.heightIn(min = 48.dp)) { Text(title) }
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun PushSettingsCard(settings: PushSettingsDto, busy: Boolean, onUpdate: (PushPreferencesUpdateDto) -> Unit) {
    val prefs = settings.preferences
    val editable = settings.can_manage && !busy
    val channel = settings.channels.firstOrNull { it.kind == "fcm" }
    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(22.dp),
        colors = CardDefaults.cardColors(containerColor = CardColor),
    ) {
        Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text("PUSH NOTIFICATION SETTINGS", color = Color(0xFFA98CE6), fontSize = 12.sp, fontWeight = FontWeight.Bold)
            Text(channel?.let { "This phone · Android app · ${it.status}" } ?: "This phone is not registered for push yet.", color = Muted, fontSize = 13.sp)
            if (!settings.capabilities.delivery_active) {
                Text("Push delivery is not active on this RoomOS server yet. Choices are saved for later.", color = Muted, fontSize = 13.sp)
            }
            if (!settings.can_manage) {
                Text("View only: the operator permission “notifications:self” is needed to change these.", color = Muted, fontSize = 13.sp)
            }
            ToggleRow("Notifications on this phone", prefs.enabled, editable) { onUpdate(PushPreferencesUpdateDto(enabled = it)) }
            ToggleRow("Critical", prefs.critical, editable) { onUpdate(PushPreferencesUpdateDto(critical = it)) }
            ToggleRow("Warning", prefs.warning, editable) { onUpdate(PushPreferencesUpdateDto(warning = it)) }
            ToggleRow("Info", prefs.info, editable) { onUpdate(PushPreferencesUpdateDto(info = it)) }
            ToggleRow("Show details on the lock screen", prefs.lock_screen_details, editable) {
                onUpdate(PushPreferencesUpdateDto(lock_screen_details = it))
            }
            ToggleRow("Quiet hours ${prefs.quiet_hours.start}–${prefs.quiet_hours.end} (${settings.capabilities.timezone})", prefs.quiet_hours.enabled, editable) {
                onUpdate(PushPreferencesUpdateDto(quiet_hours = prefs.quiet_hours.copy(enabled = it)))
            }
            ToggleRow("Critical alerts during quiet hours", prefs.quiet_hours.allow_critical, editable) {
                onUpdate(PushPreferencesUpdateDto(quiet_hours = prefs.quiet_hours.copy(allow_critical = it)))
            }
            Text("Recovery notices", color = Muted, fontSize = 13.sp)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                listOf("off" to "Off", "critical" to "Critical", "all" to "All").forEach { (value, label) ->
                    OutlinedButton(
                        onClick = { onUpdate(PushPreferencesUpdateDto(recovery = value)) },
                        enabled = editable && prefs.recovery != value,
                        modifier = Modifier.heightIn(min = 48.dp),
                    ) { Text(if (prefs.recovery == value) "✓ $label" else label) }
                }
            }
            OutlinedButton(
                onClick = { onUpdate(PushPreferencesUpdateDto(snooze_minutes = if (prefs.snooze_until != null) 0 else 60)) },
                enabled = editable && prefs.enabled,
                modifier = Modifier.heightIn(min = 48.dp),
            ) { Text(if (prefs.snooze_until != null) "Resume non-critical push" else "Pause non-critical push for 1 hour") }
            settings.recent.takeLast(5).reversed().forEach {
                Text("• ${deliveryLabel(it.status)} · ${it.severity}", color = Muted, fontSize = 12.sp)
            }
        }
    }
}

@Composable
private fun ToggleRow(label: String, checked: Boolean, enabled: Boolean, onChange: (Boolean) -> Unit) {
    Row(
        modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Text(label, modifier = Modifier.weight(1f), fontSize = 14.sp)
        Switch(checked = checked, onCheckedChange = onChange, enabled = enabled)
    }
}
