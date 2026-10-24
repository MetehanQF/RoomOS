package com.metehantech.roomos

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.metehantech.roomos.data.AuthorizationRequired
import com.metehantech.roomos.data.ConnectionState
import com.metehantech.roomos.data.PushPreferencesUpdateDto
import com.metehantech.roomos.data.PushBanner
import com.metehantech.roomos.data.Screen
import com.metehantech.roomos.data.RoomOsRepository
import com.metehantech.roomos.data.RoomOsUiState
import com.metehantech.roomos.telemetry.TelemetryController
import com.metehantech.roomos.telemetry.TelemetryPort
import com.metehantech.roomos.push.RoomOsPush
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.random.Random
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener

class RoomOsViewModel(
    private val repository: RoomOsRepository,
    private val reconnectPolicy: ReconnectPolicy = ReconnectPolicy(),
    telemetryPort: TelemetryPort? = null,
) : ViewModel() {
    private val mutableState = MutableStateFlow(
        RoomOsUiState(
            paired = repository.isPaired(),
            connection = if (repository.isPaired()) {
                ConnectionState.RECONNECTING
            } else {
                ConnectionState.AUTHORIZATION_REQUIRED
            },
        ),
    )
    val state: StateFlow<RoomOsUiState> = mutableState.asStateFlow()
    private val foreground = AtomicBoolean(false)
    private var networkAvailable = true
    private var reconnectJob: Job? = null
    private var reconnectAttempt = 0
    private val telemetry = telemetryPort?.let { port ->
        TelemetryController(viewModelScope, port, onStatus = { status ->
            mutableState.value = mutableState.value.copy(telemetry = status)
        })
    }

    fun pair(code: String) {
        if (mutableState.value.loading) return
        viewModelScope.launch {
            mutableState.value = mutableState.value.copy(loading = true, pairingError = null)
            try {
                repository.pair(code)
                mutableState.value = mutableState.value.copy(
                    paired = true,
                    loading = false,
                    connection = ConnectionState.RECONNECTING,
                )
                startConnection()
            } catch (_: Exception) {
                mutableState.value = mutableState.value.copy(
                    loading = false,
                    pairingError = "Pairing code was rejected or RoomOS is unavailable.",
                )
            }
        }
    }

    /** 7G: open the Notification Center, optionally focused on the alert a push tap named. */
    fun openNotifications(alertId: String? = null) {
        mutableState.value = mutableState.value.copy(screen = Screen.NOTIFICATIONS, focusAlertId = alertId)
        refreshNotifications()
    }

    fun closeNotifications() {
        mutableState.value = mutableState.value.copy(screen = Screen.DASHBOARD, focusAlertId = null)
    }

    /** A push arrived while RoomOS is on screen: refresh what is visible instead of a system notification. */
    fun pushWhileForeground(push: RoomOsPush) {
        mutableState.value = mutableState.value.copy(
            pushBanner = PushBanner(push.title, push.body, push.severity, push.alertId),
        )
        if (mutableState.value.screen == Screen.NOTIFICATIONS) refreshNotifications()
        else viewModelScope.launch { authoritativeRefresh() }
    }

    fun dismissPushBanner() {
        mutableState.value = mutableState.value.copy(pushBanner = null)
    }

    fun refreshNotifications() {
        if (!repository.isPaired()) return
        viewModelScope.launch {
            try {
                repository.refreshAlertScopes()
                val catalog = runCatching { repository.notifications() }.getOrNull()
                val settings = repository.pushSettings()
                val profiles = runCatching { repository.alertProfiles() }.getOrNull()
                mutableState.value = mutableState.value.copy(
                    notifications = catalog,
                    pushSettings = settings,
                    alertProfiles = profiles,
                    canSnoozeAlerts = repository.canSnoozeAlerts(),
                    canEditProfiles = repository.canEditProfiles(),
                    notificationsError = if (catalog == null) "Notification history needs Activity access." else null,
                )
            } catch (_: AuthorizationRequired) {
                authorizationRequired()
            } catch (_: Exception) {
                mutableState.value = mutableState.value.copy(notificationsError = "RoomOS is unavailable. Try again shortly.")
            }
        }
    }

    fun snoozeAlert(alertId: String, hours: Int) = alarmChange {
        repository.snoozeAlert(alertId, hours)
    }

    fun unsnoozeAlert(alertId: String) = alarmChange {
        repository.unsnoozeAlert(alertId)
    }

    fun setAlertProfile(deviceId: String, profile: String, hours: Int?) = alarmChange {
        repository.setAlertProfile(deviceId, profile, hours)
    }

    private fun alarmChange(change: suspend () -> Unit) {
        if (mutableState.value.alarmBusy) return
        viewModelScope.launch {
            mutableState.value = mutableState.value.copy(alarmBusy = true, notificationsError = null)
            try {
                change()
                mutableState.value = mutableState.value.copy(alarmBusy = false)
                refreshNotifications()
            } catch (_: AuthorizationRequired) {
                authorizationRequired()
            } catch (_: Exception) {
                mutableState.value = mutableState.value.copy(alarmBusy = false, notificationsError = "Alarm change was not saved. Try again.")
            }
        }
    }

    fun updatePush(update: PushPreferencesUpdateDto) {
        if (mutableState.value.pushBusy) return
        viewModelScope.launch {
            mutableState.value = mutableState.value.copy(pushBusy = true)
            try {
                mutableState.value = mutableState.value.copy(pushSettings = repository.updatePushSettings(update), pushBusy = false)
            } catch (_: AuthorizationRequired) {
                authorizationRequired()
            } catch (_: Exception) {
                mutableState.value = mutableState.value.copy(pushBusy = false, notificationsError = "The change was not saved.")
            }
        }
    }

    fun foreground() {
        if (!foreground.compareAndSet(false, true)) return
        if (!networkAvailable || !repository.isPaired()) return
        startConnection()
    }

    private fun startConnection() {
        reconnectJob?.cancel()
        reconnectJob = viewModelScope.launch {
            authoritativeRefresh()
            connectSocket()
        }
    }

    fun background() {
        foreground.set(false)
        reconnectJob?.cancel()
        repository.closeEvents()
        telemetry?.background(repository.isPaired() && repository.telemetryPermitted())
    }

    fun networkChanged(available: Boolean) {
        val changed = networkAvailable != available
        networkAvailable = available
        if (!changed) return
        if (!available) {
            reconnectJob?.cancel()
            repository.closeEvents()
            mutableState.value = mutableState.value.copy(connection = ConnectionState.OFFLINE)
        } else if (foreground.get()) {
            mutableState.value = mutableState.value.copy(connection = ConnectionState.RECONNECTING)
            startConnection()
        }
    }

    private suspend fun authoritativeRefresh() {
        try {
            val snapshot = repository.refresh()
            mutableState.value = mutableState.value.copy(
                paired = true,
                snapshot = snapshot,
            )
            telemetry?.sync(repository.telemetryPermitted(), foreground.get())
        } catch (_: AuthorizationRequired) {
            telemetry?.disable()
            mutableState.value = RoomOsUiState(
                paired = false,
                connection = ConnectionState.AUTHORIZATION_REQUIRED,
            )
        } catch (_: Exception) {
            mutableState.value = mutableState.value.copy(connection = ConnectionState.OFFLINE)
        }
    }

    private fun connectSocket() {
        if (!foreground.get() || !networkAvailable || !repository.isPaired()) return
        try {
            repository.openEvents(object : WebSocketListener() {
                override fun onOpen(webSocket: WebSocket, response: Response) {
                    reconnectAttempt = 0
                    mutableState.value = mutableState.value.copy(
                        connection = ConnectionState.CONNECTED,
                    )
                }

                override fun onMessage(webSocket: WebSocket, text: String) {
                    viewModelScope.launch { authoritativeRefresh() }
                }

                override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                    if (code == 4401) authorizationRequired() else scheduleReconnect()
                }

                override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                    if (response?.code == 401) authorizationRequired() else scheduleReconnect()
                }
            })
        } catch (_: AuthorizationRequired) {
            authorizationRequired()
        }
    }

    private fun authorizationRequired() {
        telemetry?.disable()
        repository.clearSession()
        reconnectJob?.cancel()
        mutableState.value = RoomOsUiState(
            paired = false,
            connection = ConnectionState.AUTHORIZATION_REQUIRED,
        )
    }

    private fun scheduleReconnect() {
        if (!foreground.get() || !networkAvailable) return
        mutableState.value = mutableState.value.copy(connection = ConnectionState.RECONNECTING)
        reconnectJob?.cancel()
        reconnectJob = viewModelScope.launch {
            val wait = reconnectPolicy.delayMillis(reconnectAttempt, Random.nextLong(0, 501))
            reconnectAttempt += 1
            delay(wait)
            authoritativeRefresh()
            connectSocket()
        }
    }

    override fun onCleared() {
        repository.closeEvents()
    }
}
