<div align="center">

<img src="web/public/icons/electronic-m-v1-512.png" alt="RoomOS" width="120" height="120">

# RoomOS

**A secure, self-hosted control layer for Home Assistant — tablet-first PWA, native Android client, push alerts.**

[![CI](https://github.com/MetehanQF/RoomOS/actions/workflows/ci.yml/badge.svg)](https://github.com/MetehanQF/RoomOS/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

</div>

> This project is an independent open-source project and is not affiliated with
> or endorsed by Cisco.

---

## What is RoomOS?

Home Assistant is very good at owning devices. It is less good at being the
thing you hand to a family member, mount on a wall tablet, or leave logged in on
a kitchen screen — its UI exposes every entity, every service call, and an
account that can rewrite your automations.

RoomOS puts a narrow, deliberate layer in front of it. Home Assistant remains
the sole owner and controller of every device; RoomOS publishes a small API for
the few things a room actually needs, and clients built only for that API.

The goal is blast-radius reduction. A RoomOS client cannot name a Home Assistant
entity, cannot call an arbitrary service, and cannot widen its own permissions —
because it never learns the vocabulary to ask.

## Features

**Control**

- **Lights, scenes, routines and automations.** Per-light power, brightness,
  colour, colour temperature and effects; a fixed scene catalog whose active
  scene is reconciled from live state; time, sun-position and presence routines;
  condition-tree automations with dry-run simulation and an explained history.
- **Capability-driven controls.** Colour, temperature and effects are offered
  only when the device advertises them *live*, not from a cached guess.
- **Honest groups.** RoomOS-owned groups expand per light, so a partial failure
  is reported per target instead of hidden behind one success.

**Monitoring and alerts**

- **System view** for two hosts, services, functional DNS health, backups and
  host-metric trends, with unavailable and stale values shown as unknown rather
  than zero.
- **Alert engine** with debounce, hysteresis and sustained windows; per-device
  alert profiles; acknowledge, mute and snooze.
- **Push notifications** over Firebase Cloud Messaging (Android) and Web Push
  (PWA), rendered server-side from fixed templates, with per-device severities,
  quiet hours and opt-in lock-screen detail.
- **Camera status** and a bounded, recent-only person snapshot. No live stream.
- **Device telemetry** for paired phones and tablets and a Windows workstation
  (via HASS.Agent), each reporting only its own declared sensors.

**Clients**

- **Tablet-first PWA** with a wall-panel mode, a customisable panel layout and an
  ambient screen. Network-only service worker: no API response or control state
  is ever cached.
- **Native Android client.** Read-only pairing, session in Android Keystore,
  device-bound FCM, own telemetry.

**Security**

- **Trusted device authentication.** Pairing is closed by default and opened
  deliberately with a single-use, expiring code. Each device has its own record
  and scopes and can be revoked individually.
- **Logical IDs only.** Clients see `room_main`, never `light.some_entity`. The
  mapping lives in one server-side registry you own.
- **Server-side permission matrix and kill switch.** Every write is re-checked in
  Core; one environment variable returns the deployment to read-only.
- **Hardened containers.** Read-only, unprivileged, all capabilities dropped,
  `no-new-privileges`, pid/memory/CPU limits.

## Architecture

Two containers on a private Docker network:

| Service | Role | Published |
| --- | --- | --- |
| `core` | FastAPI. Owns the HA token, the installation registry, the auth store, the policy engine and every scheduler. | No — internal only |
| `web` | Unprivileged nginx. Serves the built PWA and reverse-proxies `/api/`. | Yes, via `ROOMOS_BIND_ADDR` |

```text
                       Home Assistant
                    REST + WebSocket API
                             ▲
                             │ outbound only
                   ┌─────────┴─────────┐        FCM · Web Push
                   │    RoomOS Core    │ ─────▶ (optional, outbound)
                   │     (FastAPI)     │   token · registry · policy · auth
                   └─────────▲─────────┘   scenes · routines · alerts · push
                             │ private docker network
                   ┌─────────┴─────────┐
                   │    RoomOS Web     │   static PWA + same-origin proxy
                   │  (nginx, 8080)    │
                   └─────────▲─────────┘
                             │ ROOMOS_BIND_ADDR
              ┌──────────────┼──────────────┐
            Web           Android         Wear OS
           (PWA)       (read-only)       (planned)
```

Detail in [docs/architecture.md](docs/architecture.md).

## Security model

Every write crosses the same chain, in this order:

```text
Client
  ↓  HTTPS                        Secure, HttpOnly session cookies (or Keystore bearer on Android)
  ↓  Device Authentication        paired device, valid session
  ↓  Device Scopes                lights:write, routines:write, …
  ↓  Emergency Kill-switch        one variable disables all writes
  ↓  Installation Registry        logical IDs only; raw entity IDs refused
  ↓  Server-side Policy           which device, which action
  ↓  Capability / Availability    is it reachable, does it support this now
  ↓  Input Validation             ranges, bounds, live effect list
Home Assistant
```

- **The Home Assistant token, the FCM service account and the VAPID key never
  reach a client.** They exist only in Core.
- **The browser never uses raw Home Assistant entity IDs.** They appear in no
  response, event or notification. Submitting one is a 404.
- **Scenes, routines and automations obey the same policy** as a direct
  command; none can do what the policy would refuse.
- **Read-only is a supported default.** A fresh deployment controls nothing.

Full detail in [SECURITY.md](SECURITY.md), [docs/security.md](docs/security.md)
and [docs/authentication.md](docs/authentication.md).

> **Scope note.** RoomOS is designed for a trusted local network or a private
> overlay such as Tailscale. Do not expose it directly to the public internet.
> See [docs/remote-access.md](docs/remote-access.md) for the supported approach.

## Requirements

- Docker with Compose v2
- A Home Assistant instance reachable from the host
- A host on the same trusted network as your tablet

RoomOS runs comfortably on modest ARM64 or x86-64 hardware such as a Raspberry
Pi; the two containers are capped at 256 MB and 128 MB.

## Quick start

```bash
git clone https://github.com/MetehanQF/RoomOS.git
cd RoomOS
cp .env.example .env
chmod 600 .env
```

Edit `.env`: set `HA_URL`, paste a long-lived token for a **dedicated** Home
Assistant user into `HA_TOKEN`, and set `ROOMOS_AUTH_ALLOWED_ORIGIN` /
`ROOMOS_AUTH_ALLOWED_HOST` to the name your clients will use.

Describe your installation, then build and start:

```bash
cp core/app/installation.example.yaml config/installation.yaml
$EDITOR config/installation.yaml                 # map your own HA entities
echo "ROOMOS_INSTALLATION_HOST_PATH=$PWD/config/installation.yaml" >> .env

docker compose build          # tags the images with ROOMOS_IMAGE_TAG from .env
docker compose up -d
```

Pair your first device — pairing is closed until you open it:

```bash
docker compose exec core roomos pairing create
```

Open RoomOS, enter the code, and name the device. Controls stay locked until you
enable them; see [docs/configuration.md](docs/configuration.md#enabling-control).

Full procedure: [docs/installation.md](docs/installation.md).

## Configuration

Everything is environment-driven, and the entity mapping is a YAML file you own.
Nothing about your deployment belongs in tracked source.

| Variable | Default | Purpose |
| --- | --- | --- |
| `HA_URL` | `http://homeassistant.local:8123` | Home Assistant base URL |
| `HA_TOKEN` | _empty_ | Long-lived HA token. Core-only. |
| `ROOMOS_INSTALLATION_HOST_PATH` | bundled example | Your installation registry |
| `ROOMOS_BIND_ADDR` | `127.0.0.1` | Host address to publish on |
| `ROOMOS_AUTH_ALLOWED_ORIGIN` | `https://roomos.example.com` | Exact accepted origin |
| `ROOMOS_AUTH_ALLOWED_HOST` | `roomos.example.com` | Exact accepted host |
| `ROOMOS_CONTROL_ENABLED` | `false` | Global kill switch |
| `ROOMOS_CONTROL_POLICY` | `read_only` | `read_only`, `scoped`, `production` |
| `ROOMOS_ROUTINES_TIMEZONE` | `UTC` | Installation time zone |
| `ROOMOS_PUSH_MODE` | `legacy` | `legacy` or `smart` push delivery |

Every variable and the registry schema:
[docs/configuration.md](docs/configuration.md).

## Remote access

RoomOS does not need a public ingress. The supported pattern is a private
overlay network with split DNS, so the same hostname resolves to a private
address and the PWA keeps a single same-origin identity from anywhere.

See [docs/remote-access.md](docs/remote-access.md).

## Development

No Home Assistant instance and no token are needed. The suite runs entirely
against fakes, and the bundled registry points at entities that do not exist.

```bash
# Core
python3 -m venv core/.venv && core/.venv/bin/pip install -e 'core[test]'
scripts/test-core.sh -q && core/.venv/bin/python -m pytest scripts/tests -q

# Web
cd web && npm ci && npm test && npm run lint && npm run build

# Compose
cd .. && ROOMOS_IMAGE_TAG=local docker compose config -q
```

The same checks run in CI on every push and pull request, with no secrets.
More in [docs/development.md](docs/development.md).

## Roadmap

- Wear OS client, paired through the phone — [wear/README.md](wear/README.md)
- Web client following the installation time zone
- Device control beyond lighting
- Demo mode with synthetic data, for screenshots and onboarding

## Contributing

Issues and pull requests are welcome. Please read
[CONTRIBUTING.md](CONTRIBUTING.md) first — especially the rule that no real
hostname, address, entity ID, token or personal path may appear in a
contribution. CI enforces it with a privacy scan.

To report a security issue, follow [SECURITY.md](SECURITY.md) rather than
opening a public issue.

## License

[Apache License 2.0](LICENSE).
