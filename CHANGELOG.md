# Changelog

All notable changes to RoomOS are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the major version is `0`, minor releases may contain breaking changes.

## [Unreleased]

### Planned

- Wear OS client, paired through the phone.
- Web client following the installation time zone.
- Device control beyond lighting.
- General API rate limiting.
- Demo mode with synthetic data, for screenshots and contributor onboarding.

## [0.2.1] - 2026-10-24

### Fixed

- Core container image now builds on `linux/amd64`: `core/requirements.lock`
  carries PyPI sha256 hashes for the amd64 and arm64 CPython 3.12 glibc wheels.
  Previously only the arm64 hashes were locked, so `pip --require-hashes`
  rejected every amd64 wheel. Existing hashes are unchanged.
- Core tests no longer touch host state: every default under
  `/var/lib/roomos-auth/` points at a temporary directory, so the auth tests pass
  on machines where that directory does not exist.

### Added

- `scripts/lock_add_platform_hashes.py`, run by `scripts/lock-core-deps.sh`, adds
  the per-platform wheel hashes and refuses any locked hash PyPI does not publish.

## [0.2.0] - 2026-10-24

### Added

**Clients**

- Native Android client: read-only pairing profile (`android-readonly`), bearer
  session encrypted with an Android Keystore key, device-bound FCM registration,
  own-device telemetry.
- Web wall-panel mode with a per-device customisable layout, and an ambient
  screen.
- Devices view with per-device telemetry, history and short forecasts.

**Automation**

- Scenes: fixed catalog applied per light through the control policy; the active
  scene is reconciled from live light state.
- Routines: time, sun-position and presence triggers; each schedule slot is
  claimed once and survives a restart; bounded concurrency; managed example
  routines installable from the CLI.
- Automations: condition trees over device, metric and time state, cooldowns,
  dry-run simulation and a bounded history explaining every decision.
- `main_room_lights` logical group (power, brightness, colour temperature).

**Monitoring and notifications**

- Alert engine with debounce, hysteresis and sustained windows; per-device alert
  profiles; acknowledge, mute, read and snooze.
- Persisted notification incidents with dedupe and replay safety.
- Push delivery over Firebase Cloud Messaging and Web Push (VAPID): fixed
  server-side templates, per-device severities and quiet hours, opt-in
  lock-screen detail, delivery receipts. `legacy` and `smart` modes.
- Functional DNS health for two resolvers.
- Camera status and a bounded, recent-only person snapshot.
- Host-metric history and trends; Windows workstation telemetry via HASS.Agent.
- Backup tooling: encrypted runtime-state backups proven by an isolated,
  network-less restore, with a non-secret status shown in the Health view.

**Operations**

- Immutable release identity: images bound to the commit (`sha-<commit>`), OCI
  labels, and `GET /api/v1/system/version`; Web warns on a Core/Web mismatch.
- Core dependencies installed only from a hash-pinned lock.
- `roomos devices add-scope` / `remove-scope`.

### Changed

- **Breaking:** the device registry is replaced by the installation registry.
  `core/app/devices.example.yaml` and `ROOMOS_DEVICE_REGISTRY` are gone; copy
  `core/app/installation.example.yaml` to `config/installation.yaml` and set
  `ROOMOS_INSTALLATION_HOST_PATH`. It maps lights, the two monitored hosts (per
  role), Frigate, the camera and presence. Logical light IDs are now fixed:
  `room_main`, `room_2`, `room_3`, `room_4`, `desk_ambient`.
- **Breaking:** Compose requires `ROOMOS_IMAGE_TAG` (set in `.env.example`).
- Host metric IDs are `<node>_<role>` regardless of entity names.
- Time-zone defaults are `UTC` (`ROOMOS_ROUTINES_TIMEZONE`,
  `ROOMOS_PUSH_TIMEZONE`).
- DNS monitoring is off by default.

### Security

- New scopes, none granted by default web pairing: `routines:write`,
  `devices:read`, `telemetry:write:self`, `alerts:ack`, `alerts:snooze`,
  `alerts:profile`, `notifications:self`; `camera:read`, `activity:read` and
  `routines:read` are granted to the Android read-only profile.
- FCM service account and VAPID key are mounted read-only into Core only; no
  endpoint sends arbitrary notification text.
- A client may write only its own telemetry, for the device it is bound to.
- The service worker still caches nothing; it only displays Web Push.

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

[Unreleased]: https://github.com/MetehanQF/RoomOS/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/MetehanQF/RoomOS/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/MetehanQF/RoomOS/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/MetehanQF/RoomOS/releases/tag/v0.1.0
