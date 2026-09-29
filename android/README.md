# RoomOS Android client (planned)

Not implemented in v0.1.0. This records the intended architecture so the Core API
stays a workable target.

## Approach

Kotlin with Jetpack Compose, consuming the RoomOS Core REST API at `/api/v1` and
the RoomOS event WebSocket.

**The phone client must never contact Home Assistant and must never store a Home
Assistant credential.** It talks to Core and nothing else — the same constraint
the web client already meets, and what keeps the Core API small enough to audit.

## Design rules

- Address devices by stable RoomOS logical IDs. Never accept or construct a Home
  Assistant entity ID.
- Render controls from the capabilities Core reports, not a hardcoded device
  list. The server already decides what is possible.
- Treat Core's permission response as authoritative. A locally enabled button is
  not permission.
- Surface unavailable and stale state honestly, as the web client does.

## Authentication

Reuse the existing device-authentication model rather than inventing a second
one: pair with a single-use code, hold a session, carry the same scopes, and
appear in `roomos devices list` so it can be revoked like any other device.

A native client stores its session differently from a browser cookie jar. It
should use the platform keystore, and must not fall back to plain shared
preferences.

See [../docs/authentication.md](../docs/authentication.md).
