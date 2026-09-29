<div align="center">

<img src="web/public/icons/electronic-m-v1-512.png" alt="RoomOS" width="120" height="120">

# RoomOS

**A secure, self-hosted control layer and tablet-first PWA for Home Assistant.**

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
the few things a room actually needs, and a touch interface built only for that
API.

The goal is blast-radius reduction. A RoomOS client cannot name a Home Assistant
entity, cannot call an arbitrary service, and cannot widen its own permissions —
because it never learns the vocabulary to ask.

## Screenshots

> Screenshots are pending for v0.1.0. They will be captured against the
> placeholder device registry so no real deployment detail appears.

| Home | Lights | System |
| --- | --- | --- |
| _coming soon_ | _coming soon_ | _coming soon_ |

## Features

- **Trusted device authentication.** Pairing is closed by default and opened
  deliberately with a single-use, expiring code. Each paired device gets its own
  record, its own scopes, and can be revoked individually.
- **Logical device IDs only.** Clients see `living_room`, never
  `light.some_entity`. The mapping lives in one server-side registry you own.
- **Capability-driven controls.** Colour, colour temperature and effects are
  offered only when the device advertises them *live*, not from a cached guess.
- **Server-side permission matrix.** Every write is re-checked in Core. A
  modified client gains nothing.
- **Emergency kill switch.** One environment variable returns the whole
  deployment to read-only without a code change or rebuild.
- **Honest group behaviour.** The RoomOS-owned `all_room_lights` group expands
  per device, so a partial failure is reported per target instead of hidden
  behind one success.
- **Event-driven updates.** One filtered Home Assistant subscription fanned out
  to clients over a single WebSocket, with exponential-backoff reconnect.
- **Honest monitoring.** Unavailable and stale values stay visible as unknown
  rather than being rendered as zero.
- **Hardened containers.** Both services run read-only, unprivileged, with all
  capabilities dropped, `no-new-privileges`, and pid/memory/CPU limits.
- **Tablet-first PWA.** Installable, safe-area aware, with a deliberately
  network-only service worker that never caches API responses or control state.

## Architecture

Two containers on a private Docker network:

| Service | Role | Published |
| --- | --- | --- |
| `core` | FastAPI. Owns the HA token, the device registry, the auth store and the policy engine. | No — internal only |
| `web` | Unprivileged nginx. Serves the built PWA and reverse-proxies `/api/`. | Yes, via `ROOMOS_BIND_ADDR` |

```text
                       Home Assistant
                    REST + WebSocket API
                             ▲
                             │ outbound only
                   ┌─────────┴─────────┐
                   │    RoomOS Core    │   token · registry · policy · auth
                   │     (FastAPI)     │   never published on the host
                   └─────────▲─────────┘
                             │ private docker network
                   ┌─────────┴─────────┐
                   │    RoomOS Web     │   static PWA + same-origin proxy
                   │  (nginx, 8080)    │
                   └─────────▲─────────┘
                             │ ROOMOS_BIND_ADDR
              ┌──────────────┼──────────────┐
            Web           Android         Wear OS
           (PWA)         (planned)       (planned)
```

Detail in [docs/architecture.md](docs/architecture.md).

## Security model

Every request crosses the same chain, in this order:

```text
Client
  ↓  HTTPS                        Secure, HttpOnly session cookies
  ↓  Device Authentication        paired device, valid session
  ↓  Device Scopes                lights:read · lights:write · system:read
  ↓  Emergency Kill-switch        one variable disables all writes
  ↓  Logical Device Registry      logical IDs only; raw entity IDs refused
  ↓  Server-side Production Policy which device, which action
  ↓  Capability / Availability    is it reachable, does it support this now
  ↓  Input Validation             ranges, bounds, live effect list
Home Assistant
```

The properties that matter:

- **The Home Assistant token is never given to the browser.** It exists only in
  Core's process environment as a `SecretStr`, attached to outbound requests
  server-side. It is in no client bundle, cookie, or storage key.
- **The browser never uses raw Home Assistant entity IDs.** The API speaks
  logical IDs. Entity IDs appear in no response — not in the device list, the
  service list, or an error body. Submitting one is a 404.
- **Core applies a server-side allowlist and policy.** The client's UI state is
  a convenience, never the control.
- **Pairing is closed by default.** No device can join until you deliberately
  open a single-use, expiring window. Attempts are rate limited.
- **Sessions are HttpOnly and Secure**, with CSRF double-submit validation and
  an exact configurable `Origin`/`Host` check on every authenticated request.
- **Devices are individually revocable**, from the CLI, without disturbing
  others.
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

RoomOS is developed on a Raspberry Pi 5 and runs comfortably on modest ARM64 or
x86-64 hardware; the two containers are capped at 256 MB and 128 MB.

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

Describe your devices, then start:

```bash
cp core/app/devices.example.yaml config/devices.yaml
$EDITOR config/devices.yaml                      # map your own HA entities
echo "ROOMOS_DEVICE_REGISTRY=$PWD/config/devices.yaml" >> .env

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

Everything is environment-driven, and the device registry is a YAML file you
own. Nothing about your deployment belongs in tracked source.

| Variable | Default | Purpose |
| --- | --- | --- |
| `HA_URL` | `http://homeassistant.local:8123` | Home Assistant base URL |
| `HA_TOKEN` | _empty_ | Long-lived HA token. Core-only. |
| `ROOMOS_DEVICE_REGISTRY` | bundled example | Path to your device registry |
| `ROOMOS_BIND_ADDR` | `127.0.0.1` | Host address to publish on |
| `ROOMOS_AUTH_ENABLED` | `true` | Device authentication |
| `ROOMOS_AUTH_ALLOWED_ORIGIN` | `https://roomos.example.com` | Exact accepted origin |
| `ROOMOS_AUTH_ALLOWED_HOST` | `roomos.example.com` | Exact accepted host |
| `ROOMOS_CONTROL_ENABLED` | `false` | Global kill switch |
| `ROOMOS_CONTROL_POLICY` | `read_only` | `read_only`, `scoped`, `production` |

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
cd core && python3 -m venv .venv && .venv/bin/pip install -e '.[test]' && .venv/bin/pytest

# Web
cd ../web && npm ci && npm test && npm run lint && npm run build

# Compose
cd .. && docker compose config && docker compose build
```

The same checks run in CI on every push and pull request, with no secrets.
More in [docs/development.md](docs/development.md).

## Roadmap

- Android client over the same Core API — [android/README.md](android/README.md)
- Wear OS client, paired through the phone — [wear/README.md](wear/README.md)
- Device classes beyond lighting
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
