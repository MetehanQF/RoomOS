# Configuring RoomOS

Everything deployment-specific is environment-driven or lives in an installation
registry you own. Nothing about your house belongs in tracked source.

Set variables in `.env` at the repository root. `.env` is git-ignored; keep it
mode `0600`. `.env.example` lists the values you will normally set; this page
lists the rest.

## Home Assistant

| Variable | Default | Description |
| --- | --- | --- |
| `HA_URL` | `http://homeassistant.local:8123` | Base URL, as reachable from the Core container. |
| `HA_TOKEN` | _empty_ | Long-lived token for a dedicated HA user. Held only by Core, wrapped in `SecretStr`, never sent to a client. |
| `ROOMOS_HA_CONNECT_ENABLED` | `false` | Open the live REST/WebSocket connection. Reads only. |
| `ROOMOS_HA_RECONNECT_MIN_SECONDS` | `1` | Initial reconnect backoff. |
| `ROOMOS_HA_RECONNECT_MAX_SECONDS` | `60` | Backoff ceiling. |

With `HA_TOKEN` empty Core still starts and serves `/api/v1/health`, reporting
`not_configured` and making no Home Assistant calls. That is a valid development
state.

## Installation registry

| Variable | Default | Description |
| --- | --- | --- |
| `ROOMOS_INSTALLATION_HOST_PATH` | bundled example | Host path Compose mounts read-only into Core. |
| `ROOMOS_INSTALLATION_PATH` | bundled example | Path inside Core. Compose sets it; set it yourself only outside Compose. |

The registry is the single place where RoomOS logical IDs are mapped onto your
Home Assistant entities. Clients only ever see logical IDs (`room_main`,
`primary`, `room_camera`); entity IDs never leave Core and are refused if a client
submits one.

```bash
cp core/app/installation.example.yaml config/installation.yaml
$EDITOR config/installation.yaml
echo "ROOMOS_INSTALLATION_HOST_PATH=$PWD/config/installation.yaml" >> .env
```

The bundled example maps entities that do not exist, so a fresh checkout is safe
but inert, and Core logs a warning while it is in use. The example file is the
reference: every key is commented there.

### Sections

| Section | Logical IDs | What you map |
| --- | --- | --- |
| `lights` | `room_main`, `room_2`, `room_3`, `room_4`, `desk_ambient` | One `light.*` entity each, display name, Kelvin range, whether effects are allowed. |
| `light_group` | `main_room_lights` | An HA group light covering the main lights (read only) and which logical lights belong to the group. |
| `nodes` | `primary`, `secondary` | Per host, a role → entity map. Sensor roles: `cpu`, `ram`, `disk`, `temperature`, `uptime`, `nextcloud`. Binary roles: `online`, `docker`, `cloudflared`, `tailscale`, `throttled`, `smart`, `portainer`. `online` and the five numeric roles are required. |
| `frigate` | service `frigate` | Frigate integration `status` and `uptime` sensors. |
| `camera` | `room_camera` | Status sensor, last-person `input_datetime` and `input_text`, snapshot `image` entity. |
| `presence` | routine triggers | The `person` entity for home/away, a `device_tracker` used only as evidence, the `sun` entity. |

The API reports host metrics as `<node>_<role>` (`primary_cpu`), whatever your
entity is called. Logical IDs are part of the API contract; renaming one is a
breaking change for anything that stored it, so they are fixed by the code and
only the mapping and display names come from the file.

### Validation

The registry is read once at startup and validated strictly: a missing required
key, an entity in the wrong domain (`switch.*` where a `light.*` is expected), two
logical lights on one entity, or one entity mapped to two host roles stops Core
at boot with a message naming the key. RoomOS never starts with a partial
mapping. Apply a change with:

```bash
docker compose up -d --no-deps --force-recreate core
```

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

The origin and host are compared exactly, with no wildcards. If the name your
clients use changes, update both. Details in
[authentication.md](authentication.md).

## Runtime state

Everything Core persists lives in one directory, `/var/lib/roomos-auth`, a named
Compose volume mounted into Core and no other container: the device and session
database, the server secret, scenes, routines, automations and their history,
alerts, notification state, the push registry and outbox, panel layouts and the
metrics history. Each file has its own `ROOMOS_*_PATH` override, which you should
not normally need. Files are created `0600` in a `0700` directory.

Never commit, bake into an image or share this directory: it contains
authentication secrets. Back it up only encrypted — see [Backups](#backups).

## Time

| Variable | Default | Description |
| --- | --- | --- |
| `ROOMOS_ROUTINES_TIMEZONE` | `UTC` | IANA zone of the installation. Drives routine schedules and sunset transitions, and how naive Home Assistant `input_datetime` values (camera events) are read. |
| `ROOMOS_PUSH_TIMEZONE` | `UTC` | Zone used for push quiet hours. |

The Web client currently formats times in a fixed zone defined in its source
(`Europe/Istanbul`). Making it follow the installation zone is a known
limitation.

## Enabling control

| Variable | Default | Description |
| --- | --- | --- |
| `ROOMOS_CONTROL_ENABLED` | `false` | Global kill switch. |
| `ROOMOS_CONTROL_POLICY` | `read_only` | `read_only`, `scoped`, `production`. |
| `ROOMOS_CONTROL_DEVICE_IDS` | _empty_ | Logical IDs, `scoped` only. |
| `ROOMOS_CONTROL_ACTIONS` | _empty_ | Actions, `scoped` only. |

Two independent gates must both open before a write succeeds: the kill switch,
and the policy. That separation means returning to safety is one variable, not
an audit of your scope lists. A device's scopes are a third, earlier gate — see
[authentication.md](authentication.md#scopes). Scenes, routines and automations
go through the same policy as a direct command; none of them can do what the
policy would refuse.

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

**`production`** — every registered light gets the full action set,
`main_room_lights` gets power, brightness and colour temperature, and
`all_room_lights` is restricted to power. The scope lists are ignored, so a typo
in them cannot widen production permissions.

Groups are expanded per member, so a partial failure is reported per target.

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

Trust `writable_permissions` in that response over the UI. The browser renders
what it was told; Core enforces.

### Returning to read-only

```bash
# .env
ROOMOS_CONTROL_ENABLED=false
```

```bash
docker compose up -d --no-deps --force-recreate core
```

## Push notifications

Optional. Two transports, both server-driven: Firebase Cloud Messaging for the
Android client and Web Push (VAPID) for the PWA.

| Variable | Default | Description |
| --- | --- | --- |
| `ROOMOS_PUSH_MODE` | `legacy` | `legacy`: only the fixed node/service/temperature incident notifications. `smart`: delivery driven by the alert engine and each device's preferences. |
| `ROOMOS_FCM_ENABLED` | `false` | Enable the FCM provider. |
| `ROOMOS_FCM_CREDENTIALS_HOST_PATH` | _empty_ | Host path to a Firebase service-account JSON. Mounted read-only into Core only. |
| `ROOMOS_WEBPUSH_VAPID_HOST_PATH` | _empty_ | Host path to the VAPID private key (PEM). Mounted read-only into Core only. |
| `ROOMOS_WEBPUSH_VAPID_SUBJECT` | `https://roomos.example.com` | VAPID `sub` claim; use your own origin. |
| `ROOMOS_WEBPUSH_EXTRA_ENDPOINT_HOSTS` | _empty_ | Extra push-service hosts to accept besides the built-in allowlist. |

Payloads are rendered server-side from fixed templates; there is no API that
sends arbitrary text to a device. Each device chooses severities and quiet hours
from its own settings, and lock-screen detail is opt-in per device.

Keep the service-account JSON and the VAPID key outside the repository, mode
`0600`, readable only by the Core container.

## Alerts

The alert engine evaluates host, service, DNS, device and resource state on the
system-monitor cadence with debounce, hysteresis and sustained-window rules, so a
single bad sample never pages anyone.

| Variable | Default | Description |
| --- | --- | --- |
| `ROOMOS_SYSTEM_MONITOR_INTERVAL_SECONDS` | `60` | Snapshot cadence. |
| `ROOMOS_ALERTS_NODE_CONFIRMATION_SECONDS` | `300` | How long a host must stay down before it is an incident. |
| `ROOMOS_ALERTS_HISTORY_LIMIT` | `300` | Resolved alerts kept. |

Per-device alert profiles (which resources matter on which device) and snoozes
are managed from the clients with the `alerts:profile` and `alerts:snooze` scopes.

## DNS health

Functional DNS probes against two resolvers you run, reported as the `dns`
service and in the system view.

| Variable | Default | Description |
| --- | --- | --- |
| `ROOMOS_DNS_MONITOR_ENABLED` | `false` | Enable the probes. |
| `ROOMOS_DNS_PRIMARY_ADDRESS` | `192.0.2.53` | First resolver (documentation address by default). |
| `ROOMOS_DNS_SECONDARY_ADDRESS` | `192.0.2.54` | Second resolver. |
| `ROOMOS_DNS_PROBE_NAME` | `example.com.` | Name queried. |
| `ROOMOS_DNS_PROBE_INTERVAL_SECONDS` | `60` | Cadence. |
| `ROOMOS_DNS_PROBE_TIMEOUT_SECONDS` | `2` | Per-query timeout; one lost reply is retried before a resolver counts as offline. |

## Camera

Read-only status of one camera (`room_camera`) from the entities in the
registry, plus a bounded snapshot endpoint that serves the latest person image
only while the detection is recent. Live streaming is not provided.

| Variable | Default | Description |
| --- | --- | --- |
| `ROOMOS_CAMERA_REFRESH_INTERVAL_SECONDS` | `60` | Status refresh cadence. |
| `ROOMOS_CAMERA_EVENT_RECENT_HOURS` | `24` | How long a person event counts as recent. |

## Device telemetry

The Devices view combines the monitored hosts with clients that report about
themselves.

| Variable | Default | Description |
| --- | --- | --- |
| `ROOMOS_DEVICES_CLIENT_TELEMETRY` | _empty_ | JSON map binding a paired client (its auth device id) to a catalog device: `{"<device-id>": {"device": "phone", "app": "android"}}`. Unbound clients report nothing. |
| `ROOMOS_DEVICES_WORKSTATION_AGENT_ID` | _empty_ | HASS.Agent device name of a Windows workstation. |
| `ROOMOS_DEVICES_WORKSTATION_ENTITIES` | _empty_ | JSON map of RoomOS sensor ids to the entity ids HASS.Agent created; `heartbeat` is required. |
| `ROOMOS_DEVICES_WINDOWS_HEARTBEAT_SECONDS` | `120` | Heartbeat age after which the workstation counts as offline. |

A client can only write its own telemetry (`telemetry:write:self`), only for the
catalog device it is bound to, and only the sensors that device declares.
Location is deliberately not a telemetry sensor.

## Backups

`scripts/roomos-backup-run.sh` creates an encrypted backup of the runtime state,
restores it into a network-less throwaway container to prove it is usable, and
writes a non-secret `status.json`. Point `ROOMOS_BACKUP_STATUS_HOST_DIR` at the
directory holding that file and the Health view shows backup freshness. Core
never sees the backups or the key.
