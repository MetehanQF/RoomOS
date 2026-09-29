# Architecture

## Design premise

Home Assistant is the system of record for every device. RoomOS adds nothing to
the device layer — no vendor integrations, no local device protocols, no
parallel state store. It is a narrowing layer placed in front of it.

The premise is that a wall tablet and a family member's phone should not be
handed the interface that can rewrite your automations. So RoomOS publishes a
small API for what a room needs, and its clients are built only against that.

## Boundaries

RoomOS never talks to a device directly. Every command is a Home Assistant
service call; every read is a state read constrained to the device registry.
RoomOS does not touch Docker, SSH, host files, cameras, or anything else on the
host.

The registry is the only place logical IDs meet Home Assistant entities. Clients
see `living_room`; they never see or submit an entity ID, and a request carrying
one is refused with 404 before any Home Assistant call.

This is the load-bearing boundary: a fully compromised front end still cannot
name an entity it was never told about, because the vocabulary is server-side.

## Runtime topology

```text
                      ┌──────────────────────────┐
   tablet / phone ───▶ │  web  (nginx, port 8080) │
   ROOMOS_BIND_ADDR    │  static PWA + proxy      │
                      └────────────┬─────────────┘
                                   │ roomos_internal (bridge)
                      ┌────────────▼─────────────┐
                      │  core (FastAPI, 5501)    │
                      │  token · registry        │
                      │  auth · policy · events  │
                      └────────────┬─────────────┘
                                   │ outbound only
                      ┌────────────▼─────────────┐
                      │     Home Assistant       │
                      └──────────────────────────┘
```

| Component | Properties |
| --- | --- |
| `web` | Unprivileged nginx. Serves the built PWA, reverse-proxies `/api/` and the event WebSocket same-origin. The only published service. |
| `core` | FastAPI. Holds the HA token, the registry, the auth store and the policy engine. `expose`d internally, never published on the host. |
| `roomos_internal` | Project-owned bridge network. Isolated by membership, but not Docker-`internal`, because Core needs outbound access to Home Assistant. |
| `roomos_auth` | Named volume holding device records, sessions and the server secret. Created on first run. |

No bind mounts other than an optional read-only TLS pair, no Docker socket, no
host networking, no privileged mode, no device passthrough.

Because the browser only ever reaches `web`, and `web` reaches `core` over the
private network, there is no route from a client to the Home Assistant token
even if the front end is fully compromised.

### Same-origin by construction

The PWA and the API share an origin. There is no CORS configuration, no
cross-origin credential handling, and no second hostname to keep in sync. It is
also what lets session cookies be `SameSite` without special cases, and why the
`Origin` check can be an exact match rather than a policy.

## Request path for a write

Every gate below runs before a service call is issued, and each can refuse:

1. **Host check** — exact `ROOMOS_AUTH_ALLOWED_HOST`, else 403.
2. **Authentication** — a valid, unrevoked, unexpired session, else 401.
3. **Origin + CSRF** — exact origin, and a CSRF token matching both the cookie
   and the server-side session digest, else 403.
4. **Device scope** — does this device hold `lights:write`, else 403.
5. **Kill switch** — `ROOMOS_CONTROL_ENABLED`, else 403.
6. **Policy matrix** — is this device *and* action permitted, else 403.
7. **Registry lookup** — known logical ID, else 404. This is also what rejects
   raw entity IDs.
8. **Availability** — reachable right now, else 409.
9. **Live capability** — advertised by the device now, not assumed, else 422.
10. **Value validation** — brightness bounds, per-device Kelvin range, RGB
    channels, effect membership, else 422.
11. **Mapping** — logical ID translated to its entity ID; the call is made.

Only then is anything sent to Home Assistant. The result is audited with logical
IDs only.

## Authentication model

RoomOS authenticates devices, not people. A device is admitted through a
single-use, expiring pairing window opened from the host, then holds a session
cookie that is `HttpOnly`, `Secure` and `SameSite`.

The session cookie carries a session ID and a secret; the store keeps only an
HMAC digest of that secret under a server secret generated at first run. The
database alone therefore cannot be replayed against the service.

Sessions have both a sliding expiry (renewed by use) and an absolute ceiling
(never renewed), and the shorter always wins. Revocation is per device and takes
effect on the next request.

Full model: [authentication.md](authentication.md).

## The logical group

`all_room_lights` is owned by RoomOS, not delegated to a Home Assistant group.
The group command expands to each registered light and calls them individually.

That costs a round trip per device and buys honesty: when three of five lights
respond, the response is HTTP 207 listing exactly which succeeded and which
failed, and why (`unavailable` versus `service_failed`). A Home Assistant group
call reports one aggregate success and hides the two that did not move.

There is no automatic retry. A retry loop against an unresponsive bulb is how
you end up with a light that turns on ten minutes later.

## Realtime model

Core opens **one** Home Assistant WebSocket subscription to `state_changed` and
filters it against the registry. Matching events are mapped to logical IDs and
broadcast to authenticated clients over a single RoomOS WebSocket.

Clients connect to RoomOS, never to Home Assistant. They hold no Home Assistant
credentials and could not connect if they wanted to.

Reconnect backoff doubles between the configured bounds and resets after a
successful subscription. Connection-state changes are themselves broadcast, so
the UI can show an honest indicator rather than silently going stale.

An unapproved entity's state never reaches a client, even in passing — covered
by `core/tests/test_events.py`.

## Honest monitoring

Node and service monitoring reads registry-declared entities and distinguishes
three states that are often collapsed:

- **available**, with a current value
- **stale** — a value arrived, but older than five minutes
- **unavailable / unknown** — no usable value

The UI renders unknown as unknown. A panel showing `0` for a sensor that stopped
reporting is worse than one that admits it does not know.

Service entries expose a human-readable `source` label, never the backing entity
ID, so monitoring does not become a side channel for the registry.

## Technology choices

Maintained, boring components rather than custom infrastructure: Home
Assistant's own REST and WebSocket APIs, FastAPI with Pydantic and httpx, SQLite
for the auth store, React with TypeScript and Vite, and unprivileged nginx. No
message broker, no external database, no paid service.

Core holds no application state beyond the registry it loaded at startup and the
auth store. There is nothing to migrate and little to leak.

## Client architecture

The Web PWA is the reference client. Android and Wear OS are planned against the
same Core API with no additional privileges — see
[../android/README.md](../android/README.md) and
[../wear/README.md](../wear/README.md).

No client will ever receive a Home Assistant token or entity ID. That constraint
is what keeps the Core API small enough to be worth auditing.

## What is deliberately absent

- **A public ingress.** RoomOS expects a trusted network or a private overlay.
  See [remote-access.md](remote-access.md).
- **Rate limiting beyond pairing.** Pairing attempts are limited; general API
  traffic is not.
- **Device classes beyond lighting.** The capability model generalises; nothing
  else is wired up yet.
- **Certificate management.** TLS is an opt-in overlay with a certificate you
  supply.
