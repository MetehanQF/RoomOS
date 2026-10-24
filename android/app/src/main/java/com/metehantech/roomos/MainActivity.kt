package com.metehantech.roomos

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.core.content.ContextCompat
import com.metehantech.roomos.data.ConnectionState
import com.metehantech.roomos.data.DashboardSnapshot
import com.metehantech.roomos.data.PushBanner
import com.metehantech.roomos.data.Screen
import com.metehantech.roomos.data.TelemetryState
import com.metehantech.roomos.data.TelemetryStatus
import com.metehantech.roomos.push.EXTRA_ALERT_ID
import com.metehantech.roomos.push.EXTRA_MESSAGE_ID
import com.metehantech.roomos.push.ROOMOS_PUSH_TAP_ACTION
import com.metehantech.roomos.push.parsePushTap
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

class MainActivity : ComponentActivity() {
    private val viewModel: RoomOsViewModel by viewModels {
        (application as RoomOsApplication).let { RoomOsViewModelFactory(it.repository, it.telemetryPort) }
    }
    private lateinit var connectivity: ConnectivityManager
    private val networkCallback = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) = viewModel.networkChanged(hasUsableNetwork())
        override fun onLost(network: Network) = viewModel.networkChanged(hasUsableNetwork())
        override fun onCapabilitiesChanged(network: Network, capabilities: NetworkCapabilities) {
            viewModel.networkChanged(hasUsableNetwork())
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        connectivity = getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        lifecycle.addObserver(LifecycleEventObserver { _, event ->
            when (event) {
                Lifecycle.Event.ON_START -> viewModel.foreground()
                Lifecycle.Event.ON_STOP -> viewModel.background()
                else -> Unit
            }
        })
        setContent { RoomOsApp(viewModel) }
        handlePushIntent(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handlePushIntent(intent)
    }

    override fun onStart() {
        super.onStart()
        connectivity.registerDefaultNetworkCallback(networkCallback)
        viewModel.networkChanged(hasUsableNetwork())
        (application as RoomOsApplication).refreshFcmRegistration()
    }

    override fun onStop() {
        runCatching { connectivity.unregisterNetworkCallback(networkCallback) }
        super.onStop()
    }

    // VPN transports such as Tailscale do not consistently expose VALIDATED even when
    // the fixed RoomOS origin is reachable. Treat Android connectivity as a hint and let
    // the authoritative HTTPS/WSS requests decide Connected vs Offline.
    private fun hasUsableNetwork(): Boolean = connectivity.activeNetwork
        ?.let(connectivity::getNetworkCapabilities)
        ?.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) == true

    private fun handlePushIntent(incoming: Intent?) {
        if (incoming == null) return
        val navigation = parsePushTap(
            incoming.action,
            incoming.dataString,
            incoming.getStringExtra(EXTRA_ALERT_ID),
            incoming.getStringExtra(EXTRA_MESSAGE_ID),
        ) ?: return
        // Consume before navigation/network work so recreation or repeated lifecycle
        // delivery cannot manufacture a second user-open action.
        if (incoming.action == ROOMOS_PUSH_TAP_ACTION) {
            incoming.action = null
            incoming.data = null
        }
        incoming.removeExtra(EXTRA_ALERT_ID)
        incoming.removeExtra(EXTRA_MESSAGE_ID)
        val app = application as RoomOsApplication
        if (!app.repository.isPaired()) return
        viewModel.openNotifications(navigation.alertId)
        navigation.messageId?.let { app.sendPushReceipt(it, "opened") }
    }
}

private val RoomOsColors = darkColorScheme(
    primary = Color(0xFFB08CFF),
    secondary = Color(0xFF7D5CFF),
    background = Color(0xFF07060B),
    surface = Color(0xFF151120),
    onPrimary = Color(0xFF130A25),
    onBackground = Color(0xFFF4F0FF),
    onSurface = Color(0xFFF4F0FF),
)

@Composable
fun RoomOsApp(viewModel: RoomOsViewModel) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val application = LocalContext.current.applicationContext as RoomOsApplication
    LaunchedEffect(state.paired) {
        if (state.paired) application.refreshFcmRegistration()
    }
    LaunchedEffect(application, viewModel) {
        application.pushEvents.collect(viewModel::pushWhileForeground)
    }
    MaterialTheme(colorScheme = RoomOsColors) {
        Surface(modifier = Modifier.fillMaxSize()) {
            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .background(
                        Brush.radialGradient(
                            listOf(Color(0xFF25153C), Color(0xFF07060B)),
                            radius = 1_300f,
                        ),
                    ),
            ) {
                if (!state.paired) PairingScreen(state.loading, state.pairingError, viewModel::pair)
                else when (state.screen) {
                    Screen.NOTIFICATIONS -> NotificationsScreen(
                        state,
                        viewModel::closeNotifications,
                        viewModel::refreshNotifications,
                        viewModel::updatePush,
                        viewModel::snoozeAlert,
                        viewModel::unsnoozeAlert,
                        viewModel::setAlertProfile,
                    )
                    Screen.DASHBOARD -> DashboardScreen(
                        state.connection,
                        state.snapshot,
                        state.telemetry,
                        state.pushBanner,
                        viewModel::openNotifications,
                        viewModel::dismissPushBanner,
                        application::refreshFcmRegistration,
                    )
                }
            }
        }
    }
}

@Composable
private fun PairingScreen(loading: Boolean, error: String?, onPair: (String) -> Unit) {
    var code by remember { mutableStateOf("") }
    Column(
        modifier = Modifier.fillMaxSize().padding(28.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text("RoomOS", fontSize = 36.sp, fontWeight = FontWeight.SemiBold)
        Spacer(Modifier.height(8.dp))
        Text("Securely pair this Android device", color = Color(0xFFBDB4CE))
        Spacer(Modifier.height(24.dp))
        OutlinedTextField(
            value = code,
            onValueChange = { code = it.take(32) },
            label = { Text("One-time pairing code") },
            singleLine = true,
            keyboardOptions = KeyboardOptions(capitalization = KeyboardCapitalization.Characters),
            enabled = !loading,
            modifier = Modifier.fillMaxWidth(0.7f),
        )
        if (error != null) {
            Spacer(Modifier.height(10.dp))
            Text(error, color = MaterialTheme.colorScheme.error)
        }
        Spacer(Modifier.height(18.dp))
        Button(
            onClick = { onPair(code) },
            enabled = !loading && code.filter(Char::isLetterOrDigit).length in 8..32,
            modifier = Modifier.height(48.dp),
            colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF9A73FF)),
        ) {
            if (loading) {
                CircularProgressIndicator(modifier = Modifier.width(20.dp), strokeWidth = 2.dp)
                Spacer(Modifier.width(10.dp))
            }
            Text(if (loading) "Pairing…" else "Pair device")
        }
    }
}

@Composable
private fun DashboardScreen(
    connection: ConnectionState,
    snapshot: DashboardSnapshot?,
    telemetry: TelemetryStatus,
    pushBanner: PushBanner?,
    onOpenNotifications: (String?) -> Unit,
    onDismissPushBanner: () -> Unit,
    onNotificationPermissionGranted: () -> Unit,
) {
    Column(
        modifier = Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Column {
                Text("RoomOS", fontSize = 34.sp, fontWeight = FontWeight.SemiBold)
                Text("Native read-only client", color = Color(0xFFBDB4CE))
            }
            ConnectionPill(connection)
        }
        if (snapshot == null) {
            DashboardCard("Home", "Waiting for authoritative RoomOS state…", Modifier.fillMaxWidth())
            return@Column
        }
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(14.dp)) {
            DashboardCard("Home", "Lights ${snapshot.lightsOn} / ${snapshot.lightsTotal} on\nScene ${snapshot.currentScene}", Modifier.weight(1f))
            DashboardCard("Alerts", "${snapshot.activeAlerts} active\n${snapshot.highestSeverity ?: "No active severity"}", Modifier.weight(1f))
        }
        pushBanner?.let {
            ForegroundPushCard(it, onOpen = { onOpenNotifications(it.alertId) }, onDismiss = onDismissPushBanner)
        }
        Button(
            onClick = { onOpenNotifications(null) },
            modifier = Modifier.fillMaxWidth().height(48.dp),
        ) { Text("Open notifications") }
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(14.dp)) {
            DashboardCard("System", "Primary ${snapshot.primary}\nSecondary ${snapshot.secondary}\nDNS ${snapshot.dns}", Modifier.weight(1f))
            DashboardCard("Camera", "${snapshot.camera}\nDetection ${snapshot.detection}", Modifier.weight(1f))
        }
        TelemetryCard(telemetry)
        NotificationPermissionCard(onNotificationPermissionGranted)
        Text(
            "Read-only · Notification delivery remains server controlled",
            color = Color(0xFF958CA8),
            fontSize = 13.sp,
        )
    }
}

@Composable
private fun ForegroundPushCard(push: PushBanner, onOpen: () -> Unit, onDismiss: () -> Unit) {
    Card(
        modifier = Modifier.fillMaxWidth().clickable(onClick = onOpen),
        shape = RoundedCornerShape(22.dp),
        colors = CardDefaults.cardColors(containerColor = severityColor(push.severity).copy(alpha = 0.18f)),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth().padding(18.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text(push.title, fontWeight = FontWeight.SemiBold)
                Text(push.body, color = Color(0xFFD8CFE5), fontSize = 13.sp)
            }
            TextButton(onClick = onDismiss, modifier = Modifier.height(48.dp)) { Text("Dismiss") }
        }
    }
}

@Composable
private fun NotificationPermissionCard(onGranted: () -> Unit) {
    if (android.os.Build.VERSION.SDK_INT < 33) return
    val context = LocalContext.current
    var granted by remember {
        mutableStateOf(
            ContextCompat.checkSelfPermission(
                context,
                Manifest.permission.POST_NOTIFICATIONS,
            ) == PackageManager.PERMISSION_GRANTED,
        )
    }
    val launcher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { accepted ->
        granted = accepted
        if (accepted) onGranted()
    }
    if (granted) return
    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(22.dp),
        colors = CardDefaults.cardColors(containerColor = Color(0xCC171222)),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth().padding(18.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text("Notifications are off", fontWeight = FontWeight.SemiBold)
                Text(
                    "Allow RoomOS critical and warning alerts. The app remains usable if declined.",
                    color = Color(0xFFBDB4CE),
                    fontSize = 13.sp,
                )
            }
            Spacer(Modifier.width(14.dp))
            Button(
                onClick = { launcher.launch(Manifest.permission.POST_NOTIFICATIONS) },
                modifier = Modifier.height(48.dp),
            ) {
                Text("Allow")
            }
        }
    }
}

@Composable
private fun TelemetryCard(telemetry: TelemetryStatus) {
    val sent = telemetry.lastSentAtMillis?.let {
        DateTimeFormatter.ofPattern("HH:mm:ss").withZone(ZoneId.systemDefault()).format(Instant.ofEpochMilli(it))
    }
    val text = when (telemetry.state) {
        TelemetryState.NOT_ENABLED ->
            "Not shared. The RoomOS operator has not enabled device telemetry for this app."
        TelemetryState.ACTIVE ->
            "Battery, memory, storage and network are shared with RoomOS every minute while the app is open " +
                "and about every 15 minutes in the background. Last sent $sent."
        TelemetryState.PROBLEM ->
            "The last report could not be sent${sent?.let { " (last success $it)" } ?: ""}. " +
                "RoomOS will show older values as stale; the next report measures again."
    }
    DashboardCard("Device telemetry", text, Modifier.fillMaxWidth(), valueSize = 15)
}

@Composable
private fun ConnectionPill(connection: ConnectionState) {
    val (label, color) = when (connection) {
        ConnectionState.CONNECTED -> "Connected" to Color(0xFF79D7A0)
        ConnectionState.RECONNECTING -> "Reconnecting" to Color(0xFFF0C76B)
        ConnectionState.OFFLINE -> "Offline" to Color(0xFFE58C8C)
        ConnectionState.AUTHORIZATION_REQUIRED -> "Authorization required" to Color(0xFFF0C76B)
    }
    Surface(color = color.copy(alpha = 0.14f), shape = RoundedCornerShape(999.dp)) {
        Text(label, color = color, modifier = Modifier.padding(horizontal = 14.dp, vertical = 9.dp), fontWeight = FontWeight.Medium)
    }
}

@Composable
private fun DashboardCard(title: String, value: String, modifier: Modifier = Modifier, valueSize: Int = 19) {
    Card(
        modifier = modifier,
        shape = RoundedCornerShape(22.dp),
        colors = CardDefaults.cardColors(containerColor = Color(0xCC171222)),
    ) {
        Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(title.uppercase(), color = Color(0xFFA98CE6), fontSize = 12.sp, fontWeight = FontWeight.Bold)
            Text(value, fontSize = valueSize.sp, lineHeight = (valueSize * 1.45f).sp)
        }
    }
}
