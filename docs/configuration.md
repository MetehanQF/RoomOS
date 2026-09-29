# Configuring RoomOS

Everything deployment-specific is environment-driven or lives in a device
registry you own. Nothing about your house belongs in tracked source.

Set variables in `.env` at the repository root. `.env` is git-ignored; keep it
mode `0600`.

## Home Assistant

| Variable | Default | Description |
| --- | --- | --- |
| `HA_URL` | `http://homeassistant.local:8123` | Base URL, as reachable from the Core container. |
| `HA_TOKEN` | _empty_ | Long-lived token for a dedicated HA user. Held only by Core, wrapped in `SecretStr`, never sent to a client. |

With `HA_TOKEN` empty Core still starts and serves `/api/v1/health`, reporting
`not_configured` and making no Home Assistant calls. That is a valid development
state.

## Device registry

| Variable | Default | Description |
| --- | --- | --- |
| `ROOMOS_DEVICE_REGISTRY` | bundled example | Path to your registry YAML. |

The bundled example maps entities that do not exist, so a fresh checkout is safe
but inert.

```bash
cp core/app/devices.example.yaml config/devices.yaml
$EDITOR config/devices.yaml
echo "ROOMOS_DEVICE_REGISTRY=$PWD/config/devices.yaml" >> .env
```

### Schema

```yaml
devices:
  living_room:
    entity_id: light.your_entity_here
    name: Living Room
    min_kelvin: 2500
    max_kelvin: 6500
    supports_effects: true

group:
  excluded_entity_id: light.your_ha_group_here

nodes:
  primary:
    name: Primary Node
    entities:
      - sensor.primary_cpu
      - binary_sensor.primary_online

services:
  - id: home_assistant
    source: RoomOS HA API
  - id: container_runtime
    entity_id: binary_sensor.primary_container_runtime
    source: Primary node binary sensor
```

#### `devices`

Each key is a logical device ID exposed to clients. Treat them as stable:
renaming one is a breaking change for anything that stored it.

| Field | Required | Description |
| --- | --- | --- |
| `entity_id` | yes | The Home Assistant entity. Never leaves Core. |
| `name` | no | Display name sent to clients. Defaults to a title-cased ID. |
| `min_kelvin` / `max_kelvin` | no | Bounds for `temperature`, validated server-side. Default 2500–6500. |
| `supports_effects` | no | `false` refuses effects regardless of what the device reports. Default `true`. |

Every listed device becomes a member of the RoomOS-owned `all_room_lights`
group.

#### `group`

Optional. If Home Assistant has a group entity covering the same lights, name it
as `excluded_entity_id`. RoomOS records it specifically to keep it out of the
writable surface — the group is expanded per device instead, which is what makes
partial-failure reporting possible.

#### `nodes`

Optional host monitoring. Use generic IDs (`primary`, `secondary`), not
hostnames: these appear in API routes and are sent to clients.

Metric keys are the entity ID minus its domain, so `sensor.primary_cpu` becomes
metric `primary_cpu`. The UI looks for `<node>_cpu`, `_ram`, `_disk`,
`_temperature`, `_uptime`, and uses `<node>_online` as the node state. Anything
else you list is still exposed by the API. Omit the section for no monitoring.

#### `services`

Optional service health strip.

| Field | Required | Description |
| --- | --- | --- |
| `id` | yes | Logical service ID shown to clients. |
| `entity_id` | no | Backing binary sensor. Omit for a static entry. |
| `source` | no | Human-readable provenance label. |

`source` must never contain a Home Assistant entity ID — it is sent to the
browser.

### Applying changes

The registry is read once at startup:

```bash
docker compose up -d --no-deps --force-recreate core
```

A malformed registry stops Core at boot rather than starting with a partial
device list.

## Exposure

| Variable | Default | Description |
| --- | --- | --- |
| `ROOMOS_BIND_ADDR` | `127.0.0.1` | Host address Compose publishes the web UI on. |
| `ROOMOS_HTTP_PORT` | `5500` | Published HTTP port. |
| `ROOMOS_TLS_CERT` | _empty_ | Path to `fullchain.pem`, required by `compose.tls.yaml`. |
| `ROOMOS_TLS_KEY` | _empty_ | Path to `privkey.pem`, required by `compose.tls.yaml`. |
| `ROOMOS_HTTPS_PORT` | `8443` | Published HTTPS port with the TLS overlay. |

**Never set `ROOMOS_BIND_ADDR=0.0.0.0`.** Core is deliberately never published
on the host; it is reachable only from `web` over the private Docker network.

## Authentication

| Variable | Default | Description |
| --- | --- | --- |
| `ROOMOS_AUTH_ENABLED` | `true` | Device authentication. Disable only for local development. |
| `ROOMOS_AUTH_ALLOWED_ORIGIN` | `https://roomos.example.com` | Exact origin accepted for unsafe requests. |
| `ROOMOS_AUTH_ALLOWED_HOST` | `roomos.example.com` | Exact `Host` accepted for authenticated requests. |
| `ROOMOS_AUTH_SESSION_SLIDING_DAYS` | `30` | Renews on use. 1–90. |
| `ROOMOS_AUTH_SESSION_ABSOLUTE_DAYS` | `180` | Hard ceiling from pairing. 7–365. |
| `ROOMOS_AUTH_PAIRING_TTL_SECONDS` | `600` | Pairing window lifetime. 60–1800. |
| `ROOMOS_AUTH_PAIRING_ATTEMPTS` | `5` | Attempts allowed per window. 1–20. |
| `ROOMOS_AUTH_PAIRING_WINDOW_SECONDS` | `300` | Rate-limit window. 60–1800. |
| `ROOMOS_AUTH_DATABASE_PATH` | `data/roomos-auth.db` | Device and session store. A named volume under Compose. |
| `ROOMOS_AUTH_SECRET_PATH` | `data/server-secret` | Server secret, generated at first run, mode `0600`. |

The origin and host are compared exactly, with no wildcards. If the name your
clients use changes, update both. Details in
[authentication.md](authentication.md).

Never commit the auth database or server secret. `data/`, `*.db` and
`server-secret` are git-ignored.

## Runtime

| Variable | Default | Description |
| --- | --- | --- |
| `ROOMOS_LOG_LEVEL` | `INFO` | Standard Python log level. |
| `ROOMOS_HA_CONNECT_ENABLED` | `false` | Open the live REST/WebSocket connection. Reads only. |
| `ROOMOS_HA_RECONNECT_MIN_SECONDS` | `1` | Initial reconnect backoff. |
| `ROOMOS_HA_RECONNECT_MAX_SECONDS` | `60` | Backoff ceiling. |

## Enabling control

| Variable | Default | Description |
| --- | --- | --- |
| `ROOMOS_CONTROL_ENABLED` | `false` | Global kill switch. |
| `ROOMOS_CONTROL_POLICY` | `read_only` | `read_only`, `scoped`, `production`. |
| `ROOMOS_CONTROL_DEVICE_IDS` | _empty_ | Logical IDs, `scoped` only. |
| `ROOMOS_CONTROL_ACTIONS` | _empty_ | Actions, `scoped` only. |

Two independent gates must both open before a write succeeds: the kill switch,
and the policy. That separation means returning to safety is one variable, not
an audit of your scope lists.

A device's scopes are a third, earlier gate — see
[authentication.md](authentication.md#scopes).

### Policy modes

**`read_only`** (default) — nothing writable, even with the kill switch on.

**`scoped`** — only the intersection of the two lists. Use it to introduce one
device at a time; unknown IDs and actions are ignored rather than granted.

```bash
ROOMOS_CONTROL_ENABLED=true
ROOMOS_CONTROL_POLICY=scoped
ROOMOS_CONTROL_DEVICE_IDS=desk_ambient
ROOMOS_CONTROL_ACTIONS=power,brightness
```

**`production`** — every registered device gets the full action set, and
`all_room_lights` is restricted to `power`. The scope lists are ignored, so a
typo in them cannot widen production permissions.

The group stays power-only on purpose: a brightness or colour sweep across a
whole room is rarely what someone meant to press, and a partial failure
mid-sweep is hard to reason about.

### Actions

| Action | Route | Notes |
| --- | --- | --- |
| `power` | `POST /api/v1/lights/{id}/power` | `{"on": bool}` |
| `brightness` | `POST /api/v1/lights/{id}/brightness` | 1–255 |
| `color` | `POST /api/v1/lights/{id}/color` | RGB, or hue+saturation — not both |
| `temperature` | `POST /api/v1/lights/{id}/temperature` | Kelvin, within the device's range |
| `effect` | `POST /api/v1/lights/{id}/effect` | Must be in the live effect list |

A granted action still fails with 422 if the capability is not currently
advertised, and 409 if the device is unavailable. Policy grants permission; the
device decides what is possible.

### Verifying

```bash
curl -s http://127.0.0.1:5500/api/v1/health
```

```json
{
  "control_enabled": true,
  "control_policy": "scoped",
  "writable_device_ids": ["desk_ambient"],
  "writable_actions": ["brightness", "power"],
  "writable_permissions": { "desk_ambient": ["brightness", "power"] }
}
```

Trust this over the UI. The browser renders what it was told; Core enforces.

### Returning to read-only

```bash
# .env
ROOMOS_CONTROL_ENABLED=false
```

```bash
docker compose up -d --no-deps --force-recreate core
```
