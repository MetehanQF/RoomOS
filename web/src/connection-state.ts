import type { ConnectionStatus } from './types'

let current: ConnectionStatus = 'reconnecting'

export function connectionStatus() {
  return current
}

export function setConnectionStatus(status: ConnectionStatus) {
  current = status
}

export function resetConnectionStatus() {
  current = 'reconnecting'
}
