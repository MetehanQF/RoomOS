package com.metehantech.roomos

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import com.metehantech.roomos.data.RoomOsRepository
import com.metehantech.roomos.telemetry.TelemetryPort

class RoomOsViewModelFactory(
    private val repository: RoomOsRepository,
    private val telemetryPort: TelemetryPort? = null,
) : ViewModelProvider.Factory {
    @Suppress("UNCHECKED_CAST")
    override fun <T : ViewModel> create(modelClass: Class<T>): T =
        RoomOsViewModel(repository, telemetryPort = telemetryPort) as T
}
