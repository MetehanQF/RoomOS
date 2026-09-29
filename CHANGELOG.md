# Changelog

All notable changes to RoomOS are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the major version is `0`, minor releases may contain breaking changes.

## [Unreleased]

### Planned

- Android client over the same Core API.
- Wear OS client, paired through the phone.
- Device classes beyond lighting.
- Per-device scope editing from the CLI.
- General API rate limiting.
- Demo mode with synthetic data, for screenshots and contributor onboarding.

## [0.1.0] - 2026-09-29

Initial public release.

### Added

**Authentication**

- Device authentication with closed-by-default pairing: single-use, expiring,
  rate-limited codes issued from the host CLI.
- Per-device records with scopes (`lights:read`, `lights:write`, `system:read`),
  individually revocable.
- Sessions as `HttpOnly`, `Secure`, `SameSite` cookies, with both sliding and
  absolute expiry.
- Session secrets, CSRF tokens and pairing codes stored only as HMAC digests
  under a server secret generated at first run, compared in constant time.
- Exact `Host` and `Origin` pinning, plus CSRF double-submit bound to the
  server-side session record.
- `roomos` CLI: `pairing create`, `devices list`, `devices revoke`,
  `devices revoke-sessions`, `devices disable`.
- Security audit stream covering pairing, authentication, CSRF, host and origin
  rejections, and revocation.

**Core**

- Home Assistant abstraction over REST and WebSocket. Core holds the token;
  clients never see it.
- Config-driven YAML device registry mapping logical IDs onto Home Assistant
  entities, selected with `ROOMOS_DEVICE_REGISTRY`. A placeholder registry ships
  as `core/app/devices.example.yaml`.
- Capability-driven lighting: power, brightness, RGB colour, colour temperature
  and effects, each permitted only when advertised live, with per-device Kelvin
  ranges.
- Server-side permission matrix (`read_only`, `scoped`, `production`) evaluated
  on every write, plus a global kill switch overriding all of them.
- RoomOS-owned `all_room_lights` group that expands per device and reports
  partial failures per target, with no retry loop.
- Event bridge: one filtered Home Assistant subscription fanned out to clients
  over a single WebSocket, with exponential-backoff reconnect.
- Raw Home Assistant entity IDs and unregistered device IDs rejected with 404
  before any Home Assistant call.
- Sanitised command audit logging.
- Registry-driven node and service monitoring, exposing unavailable and stale
  values honestly as unknown.

**Web**

- Tablet-first PWA: Home, Lights and System views.
- Pairing and session-expiry flow with an authentication boundary.
- Capability-driven controls, shown only when the device supports them and the
  active policy permits them.
- Event-driven updates with a realtime connection indicator.
- Explicit partial-failure feedback for group commands.
- Runtime validation of the `/system` contract, failing before React renders.
- Installable manifest, electronic-M icon set, safe-area handling, and a
  deliberately network-only service worker that never caches API responses or
  control state.

**Deployment**

- Docker Compose stack. Both services `read_only`, unprivileged, `cap_drop: ALL`,
  `no-new-privileges`, tmpfs writable paths, and pid/memory/CPU limits.
- Core never published on the host; reachable only through the `web` proxy on a
  private network.
- Auth state in a named volume, created on first run.
- Loopback-only exposure by default, widened deliberately via
  `ROOMOS_BIND_ADDR`.
- Optional TLS overlay (`compose.tls.yaml`) using a certificate you supply.

**Project**

- Apache-2.0 license.
- Architecture, security, authentication, installation, configuration,
  remote-access and development documentation.
- CI running core tests, web tests, lint, production build, Compose and nginx
  validation, and a privacy denylist scan — with no secrets.

### Known limitations

- RoomOS is designed for a trusted LAN or a private overlay. **Do not expose it
  directly to the public internet.** See [SECURITY.md](SECURITY.md) and
  [docs/remote-access.md](docs/remote-access.md).
- No general API rate limiting; pairing attempts are limited.
- TLS is required in practice for authentication, because session cookies are
  `Secure`, and is bring-your-own.
- Lighting is the only supported device class.
- The Android and Wear OS clients are architecture notes, not implementations.

[Unreleased]: https://github.com/MetehanQF/RoomOS/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/MetehanQF/RoomOS/releases/tag/v0.1.0
