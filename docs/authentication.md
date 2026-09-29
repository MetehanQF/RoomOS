# Device authentication

RoomOS authenticates *devices*, not people. There are no usernames or passwords.
A device is admitted once, through a deliberate pairing window, and thereafter
holds its own session, scopes and revocation record.

This suits the actual deployment: a wall tablet, a phone, a kitchen screen. You
do not want a login prompt on a device that lives on a wall, but you also do not
want anyone who reaches the port to have control.

## Default posture

- Authentication is **on** (`ROOMOS_AUTH_ENABLED=true`).
- Pairing is **closed**. No device can join until you open a window.
- A pairing window is **single-use** and expires (default 10 minutes).
- Failed attempts are **rate limited** (default 5 per 5 minutes).
- Sessions are **HttpOnly, Secure, SameSite** cookies. The token is never
  readable by JavaScript.
- Every authenticated request is checked against an **exact** `Host`, and every
  unsafe request against an exact `Origin` plus a CSRF token.

Nothing about this is opt-in. A fresh deployment starts closed.

## Pairing a device

Pairing is a physical-possession gate: you must have shell access to the host to
open a window, and access to the device to consume it.

```bash
docker compose exec core roomos pairing create
```

```text
Pairing code: ABCD-EFGH-JKLM
Expires at: 2026-01-01T12:10:00+00:00
This code is single-use and will not be shown again.
```

On the device, open RoomOS, enter the code and give the device a name. The name
is a label for your own revocation decisions later — "Kitchen tablet" is more
useful than "Device 3".

The code alphabet deliberately omits `I`, `O`, `0` and `1`, because these get
typed on a tablet by someone reading them off a laptop.

Opening a new window invalidates any previous unused one: there is at most one
live pairing window at a time.

To use a shorter window:

```bash
docker compose exec core roomos pairing create --ttl-seconds 120
```

## Managing devices

```bash
docker compose exec core roomos devices list
```

Each device reports its ID, name, creation and last-seen time, enabled state and
scopes.

| Action | Command | Effect |
| --- | --- | --- |
| Sign a device out | `roomos devices revoke-sessions <id>` | Ends its sessions. It may pair again. |
| Disable a device | `roomos devices disable <id>` | Ends sessions and blocks it, reversibly. |
| Revoke a device | `roomos devices revoke <id>` | Ends sessions and permanently retires the record. |

All three take effect on the next request; there is no cache to wait out.

Use `revoke-sessions` for "I want it signed out", `disable` for "not right now",
and `revoke` for a device you no longer have — a lost or sold tablet.

## Scopes

Each device carries scopes, checked per route:

| Scope | Grants |
| --- | --- |
| `lights:read` | Reading light state |
| `lights:write` | Sending light commands |
| `system:read` | Reading node and service status |

Scopes are the *device's* ceiling. A write must additionally pass the kill
switch and the control policy, so `lights:write` is permission to ask, not
permission to act. See [configuration.md](configuration.md#enabling-control).

## Sessions

Two independent expiries, and the shorter one always wins:

- **Sliding** (default 30 days) — renews as the device is used, so a daily-use
  tablet is not signed out for no reason.
- **Absolute** (default 180 days) — a hard ceiling from pairing. It is not
  renewed by use, so every session eventually ends and has to be re-established
  deliberately.

Sliding expiry alone would mean a device in daily use never re-authenticates.
The absolute ceiling bounds that.

Nothing sensitive is stored in the session cookie. It carries a session ID and a
secret; the server holds only an HMAC digest of that secret, so the database
alone cannot be replayed against the service.

## Why the Origin and Host checks are exact

`ROOMOS_AUTH_ALLOWED_HOST` and `ROOMOS_AUTH_ALLOWED_ORIGIN` are compared
exactly. No wildcards, no suffix matching.

This is the cost of not having a public ingress: because RoomOS is reached by
one known name, that name can be pinned. A request arriving under any other
`Host` is refused before authentication, and an unsafe request with any other
`Origin` is refused before it reaches a handler. The common DNS-rebinding and
cross-origin paths simply do not have a matching value to present.

The practical consequence: if you change the name your clients use, you must
update both variables. Requests will be refused until you do — that is the
mechanism working, not a bug.

## CSRF

Unsafe requests carry a double-submit token: a cookie plus an `X-CSRF-Token`
header, which must match each other *and* the digest the server stored for that
session.

The third check matters. A plain double-submit comparison is satisfied by any
attacker who can set a cookie; binding the token to the server-side session
record means a value not issued by RoomOS for that exact session fails.

## Local development without authentication

`ROOMOS_AUTH_ENABLED=false` bypasses pairing and grants a synthetic context with
all three scopes. This exists so the suite can run and the UI can be developed
against fakes.

Never run a real deployment this way. With auth disabled, anyone who reaches the
port has whatever the control policy allows.

## If a device is lost

1. `roomos devices list` — find its ID by the name you gave it.
2. `roomos devices revoke <id>` — its sessions stop working immediately.
3. If you are unsure which record it is, revoke the candidates. A device you
   still hold can be paired again in under a minute; a device someone else holds
   cannot.

The auth store is a volume. Deleting it revokes everything and returns RoomOS to
an unpaired state — the blunt instrument, when you want to be certain.

## What is deliberately not here

- **No user accounts, roles, or password reset.** RoomOS authenticates devices.
- **No OAuth or external identity provider.** A LAN appliance should not depend
  on an internet service to unlock a light switch.
- **No remember-me beyond the absolute ceiling.**
- **No self-service pairing.** A window is opened from the host, on purpose.
