# Security model

This document covers the mechanisms. [../SECURITY.md](../SECURITY.md) covers
posture, limitations and how to report a vulnerability — read that first if you
are deciding whether to deploy. Device authentication has its own page:
[authentication.md](authentication.md).

## The two invariants

Everything else is defence in depth. These two are the design:

1. **No Home Assistant entity ID ever reaches a client** — not in a response
   body, an error detail, a log line, or an event payload.
2. **No token or session secret is ever logged or returned.**

Both are covered by tests. `core/tests/test_api.py` asserts the health endpoint
exposes no token material and that responses carry no entity IDs;
`core/tests/test_events.py` asserts unapproved entities never appear in
broadcast events; `core/tests/test_production_policy.py` asserts audit output
contains no `Authorization` header, `Bearer` value, or entity ID.

## The chain

```text
Client
  ↓  HTTPS                         Secure, HttpOnly cookies
  ↓  Device Authentication         paired device, valid session
  ↓  Device Scopes                 lights:read · lights:write · system:read
  ↓  Emergency Kill-switch         one variable disables all writes
  ↓  Logical Device Registry       logical IDs only
  ↓  Server-side Production Policy which device, which action
  ↓  Capability / Availability     reachable, and advertising it now
  ↓  Input Validation              ranges, bounds, live effect list
Home Assistant
```

Each link is independent. Removing any one of them still leaves the others
enforcing.

## Token handling

The Home Assistant token exists only in Core's process environment, wrapped in a
Pydantic `SecretStr` so it does not render in reprs, logs or tracebacks. Core
attaches it to outbound requests server-side.

It is never present in a client bundle, response body, cookie, or
`localStorage`. The browser cannot obtain it, because the browser never talks to
Home Assistant — it talks to `web`, which proxies to `core` on a private
network. Core is not published on the host at all.

Exceptions and log lines contain sanitised error classes, never request headers.

## The logical-ID boundary

Clients address devices by stable logical IDs declared in the registry, which
lives server-side in a file you own.

- Submitting a raw entity ID returns 404. RoomOS is not a Home Assistant proxy.
- Submitting an unregistered logical ID returns 404.
- Naming a Home Assistant group entity — including one listed as
  `group.excluded_entity_id` — returns 404. It is recorded precisely so it stays
  outside the writable surface.
- The service list exposes a human-readable `source` label rather than the
  backing entity ID, so monitoring is not a side channel.

A hostile client gains nothing from any of this: it never learns the vocabulary
needed to ask for something else.

## Transport and origin pinning

Session cookies are issued `HttpOnly`, `Secure` and `SameSite`, so a session is
not readable by JavaScript and does not travel over plaintext.

Every authenticated request is checked against an exact `Host`, and every unsafe
request against an exact `Origin`. No wildcards, no suffix matching.

RoomOS can afford exact matching because it is reached by one known name. That
turns a class of DNS-rebinding and cross-origin attacks into a value the
attacker cannot present. The cost is that changing the name means updating
`ROOMOS_AUTH_ALLOWED_HOST` and `ROOMOS_AUTH_ALLOWED_ORIGIN`; requests are
refused until you do.

CSRF uses a double-submit token bound to the server-side session record, not
just a cookie/header comparison — a value not issued by RoomOS for that exact
session fails, even from someone who can set cookies.

## Authorisation

Three independent gates, all of which must open.

### Device scopes

Each paired device carries scopes checked per route. Scopes are the device's
ceiling — permission to ask, not permission to act.

### The kill switch

`ROOMOS_CONTROL_ENABLED=false` makes every write route return
`403 RoomOS control is disabled`, regardless of policy, evaluated before any
handler logic.

It is operator-owned configuration and deliberately not exposed as a browser
control. No UI action can re-enable control. Returning to a safe state is one
variable, not an audit of scope lists.

### The policy matrix

| Mode | Writable surface |
| --- | --- |
| `read_only` | Nothing. |
| `scoped` | Only `ROOMOS_CONTROL_DEVICE_IDS` × `ROOMOS_CONTROL_ACTIONS`. Unknown entries are ignored, not granted. |
| `production` | Every registered device with the full action set; `all_room_lights` restricted to `power`. |

`production` is a fixed server-side matrix that does not read the free-form
scope strings, so a typo there cannot widen production permissions.

Browser state is never the authorisation boundary. A disabled button is a
courtesy; Core re-checks every request.

## Validation before contact

A granted action is still not a guaranteed call:

- **Availability** — unreachable device → 409.
- **Live capability** — must be advertised by the device *now*, read from live
  state rather than a cached assumption → 422.
- **Values** — brightness 1–255; Kelvin within that device's declared range; RGB
  channels 0–255; RGB and hue/saturation mutually exclusive; effects must appear
  in the live effect list → 422.

Per-device Kelvin ranges matter in practice: a bulb and a strip have genuinely
different ranges, and clamping both to the narrower one silently degrades the
better device, while accepting out-of-range values on the other produces a
confusing Home Assistant error instead of a clear 422.

Invalid input results in zero service calls, asserted in tests by checking the
fake Home Assistant recorded no calls.

## Audit logging

Two streams, both sanitised:

- **Command audit** — timestamp, logical target, action, result; for groups,
  which members succeeded and failed.
- **Security audit** — pairing opened, pairing succeeded or failed with a
  reason, authentication rejected, CSRF rejected, host or origin rejected,
  device disabled or revoked.

Excluded by design: tokens, session secrets, pairing codes, `Authorization`
headers, cookies, entity IDs and raw request bodies. There is no automatic
retry, so an audit line corresponds to one real attempt.

The security stream is what tells you someone is guessing pairing codes.

## Auth store at rest

Device records, sessions and pairing windows live in SQLite at
`ROOMOS_AUTH_DATABASE_PATH`; the server secret sits beside it, mode `0600`, in a
directory created `0700`.

Nothing reusable is stored in the clear: session secrets, CSRF tokens and
pairing codes are kept as HMAC digests under the server secret. Comparisons use
constant-time equality.

Deleting the store revokes everything and returns RoomOS to an unpaired state.

## Dedicated Home Assistant identity

A long-lived token inherits its user's full permissions, and Home Assistant has
no per-entity token scoping. So the account is the coarse boundary and RoomOS's
registry is the fine one.

Create a dedicated non-administrator user for RoomOS. If the token leaks,
revoking it does not disrupt your own sessions and the damage is bounded by that
user's permissions; and Home Assistant's logbook attributes RoomOS's actions to
a distinct identity, so you can tell what RoomOS did.

Never paste a token into source, a Compose file, an issue, or a screenshot. If
one is exposed, revoke it in Home Assistant first.

## Container hardening

Both services run with:

- `read_only: true` root filesystem, tmpfs for paths that need writes
- Non-root users (Core `10001`, nginx `101`)
- `cap_drop: ALL` and `no-new-privileges:true`
- `pids_limit`, `mem_limit`, `cpus` caps
- Bounded json-file logging

No Docker socket, no host networking, no privileged mode. Host mounts are
read-only: the installation registry and optional push credentials and backup
status for Core, an optional TLS pair for Web. Core is not published on the host.

## PWA transport boundary

The web client ships a manifest, local icons, standalone metadata and a
deliberately **network-only** service worker with no Cache Storage logic. Its
only other job is Web Push: it displays the notification Core rendered and opens
the app on tap. It never stores the payload.

Live API responses and control state are never served from an offline cache. A
cached control surface is actively harmful: it can show a light as on after it
was turned off, and accept a command against state that no longer exists. RoomOS
would rather show a disconnected indicator than a confident lie.

A secure origin is required for the service worker, which is another reason to
run the TLS overlay.

## Push notifications

- The FCM service account and the VAPID private key are mounted read-only into
  Core only. Clients never receive either.
- There is no endpoint that sends arbitrary text. Notifications are rendered in
  Core from fixed templates for known alert types; titles and bodies carry
  logical labels, never entity IDs, addresses or paths.
- Lock-screen detail is opt-in per device. With it off, the visible notification
  is a generic "open RoomOS" text and the detail stays inside the app.
- FCM registrations are bound to the paired device that created them; a revoked
  device's registration is dropped. Web Push endpoints are accepted only from an
  allowlist of push-service hosts.
- Delivery uses a persisted outbox with dedupe, so a restart never replays an
  old alert as new.

## Camera

RoomOS reads camera status from Home Assistant and exposes one logical camera.
The snapshot endpoint serves only the latest person image, only while the
detection is recent, with `Cache-Control: no-store`, same-origin, behind
`camera:read`. Live streaming is not provided.

## Device telemetry

A client can report only its own telemetry (`telemetry:write:self`), only for
the catalog device it was bound to by the operator, and only the sensors that
device declares; anything else is dropped and counted. The latest values are
an in-memory cache that starts empty on every Core start; a fixed set of numeric
metrics (CPU, RAM, GPU, disk, storage, battery, temperatures) is also kept in a
bounded, downsampled SQLite history for the trends views. Location is
deliberately not a telemetry sensor and is never stored.

## Emergency read-only rollback

```bash
# .env
ROOMOS_CONTROL_ENABLED=false
ROOMOS_CONTROL_POLICY=read_only
```

```bash
docker compose up -d --no-deps --force-recreate core
```

Then confirm `/api/v1/health` reports `control_enabled: false` with empty
`writable_permissions`, that representative write routes return 403, and that
read-only REST and WebSocket updates still work.

No source change, and nothing else is restarted — only Core is recreated.

To additionally cut off every client, revoke the devices
([authentication.md](authentication.md#managing-devices)) or remove the auth
volume.
