# Security Policy

## Supported versions

RoomOS is in public preview. Only the latest tag receives fixes.

| Version | Supported |
| --- | --- |
| 0.1.x | ✅ |
| < 0.1 | ❌ |

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Use GitHub's private reporting: **Security → Report a vulnerability** on this
repository. That opens a private advisory visible only to maintainers.

Include the affected version or commit, what an attacker can achieve, and the
minimal steps to reproduce. A proof-of-concept request is more useful than a
scanner report.

Expect an acknowledgement within 7 days. This is a hobby-scale project
maintained in spare time, so please allow up to 90 days before public
disclosure, and get in touch if that timeline does not work for you.

## What RoomOS protects

**The Home Assistant token never reaches a client.** It lives only in Core's
process environment as a Pydantic `SecretStr` and is attached to outbound
requests server-side. It is absent from every client bundle, response body,
cookie and storage key.

**Clients cannot name Home Assistant entities.** The API speaks only logical IDs
from the device registry. Entity IDs appear in no response — not the device
list, the service list, or an error detail. A request carrying one is rejected
with 404 before any Home Assistant call. A fully compromised front end still
cannot address an arbitrary entity.

**Devices are authenticated and individually revocable.** Pairing is closed by
default and opened deliberately with a single-use, expiring, rate-limited code.
Each device holds its own session and scopes and can be revoked without
disturbing others.

**Sessions are hard to steal or forge.** Cookies are `HttpOnly`, `Secure` and
`SameSite`. The store holds only HMAC digests of session secrets under a server
secret generated at first run, so the database alone cannot be replayed.
Comparisons are constant-time.

**Origin and Host are pinned exactly.** Every authenticated request must arrive
under the configured `Host`; every unsafe request under the configured `Origin`,
with a CSRF token bound to the server-side session record.

**Every write is authorised server-side**, against device scopes, the kill
switch, and the policy matrix. The client's UI state is a convenience.

**There is a kill switch.** `ROOMOS_CONTROL_ENABLED=false` makes every write
route return 403 regardless of policy, with no rebuild.

**Read-only is a supported default.** A fresh deployment controls nothing.

**Input is validated before Home Assistant is contacted**, against the device's
live advertised capabilities. Invalid input results in zero service calls.

**Logs are sanitised.** Command and security audit records carry logical IDs,
actions and results — never tokens, session secrets, pairing codes,
`Authorization` headers, cookies or entity IDs.

**Containers are hardened.** Both services run `read_only`, unprivileged, with
`cap_drop: ALL`, `no-new-privileges`, tmpfs writable paths and resource limits.
Core is never published on the host.

## Known limitations in v0.1.0

**The network boundary still matters.** Device authentication raises the bar
considerably, but RoomOS is designed for a trusted LAN or a private overlay.
`ROOMOS_BIND_ADDR` defaults to loopback for this reason.

**Do not expose RoomOS directly to the public internet.** No port forwarding, no
public DNS A record, no public tunnel or funnel. Use a private overlay with
split DNS — see [docs/remote-access.md](docs/remote-access.md).

**There is no general rate limiting.** Pairing attempts are limited; ordinary
API traffic is not.

**Authentication can be disabled.** `ROOMOS_AUTH_ENABLED=false` exists for local
development against fakes. With it off, anyone who reaches the port has whatever
the control policy allows. Never use it in a real deployment.

**TLS is optional and bring-your-own.** RoomOS does not issue or renew
certificates. Without TLS, session cookies marked `Secure` will not be sent, so
authentication effectively does not work — which is the intended failure mode,
but means HTTP-only deployments are not usable with auth enabled.

**Home Assistant token scope is coarse.** A long-lived token inherits its user's
full permissions; HA has no per-entity token scoping. Create a dedicated user so
RoomOS's registry is the real boundary.

**A host-level compromise is game over.** The auth store and server secret live
on the host. Anyone with root there can mint sessions.

## Hardening checklist

- [ ] `ROOMOS_BIND_ADDR` set to a specific address, never `0.0.0.0`
- [ ] `ROOMOS_AUTH_ENABLED=true`
- [ ] `ROOMOS_AUTH_ALLOWED_ORIGIN` / `_HOST` match exactly what clients use
- [ ] TLS in front of RoomOS, so `Secure` cookies work
- [ ] A dedicated Home Assistant user, not your own account
- [ ] `HA_TOKEN` in `.env` only, untracked, mode `0600`
- [ ] `ROOMOS_CONTROL_ENABLED=false` until reads are verified
- [ ] `ROOMOS_CONTROL_POLICY` no wider than you need
- [ ] The registry contains only entities RoomOS should ever touch
- [ ] No public DNS record and no port forward for this host
- [ ] Paired devices reviewed periodically (`roomos devices list`)
- [ ] The HA token rotated if it was ever printed, pasted or committed

## If something is exposed

**A Home Assistant token:** revoke it in Home Assistant first (profile →
Security → Long-lived access tokens). Revocation ends the exposure; editing
files does not. Then replace it in `.env`, restart Core, and review the HA
logbook for calls you did not make.

**A pairing code:** codes are single-use and short-lived. Open a new window,
which invalidates any unused one, and check `roomos devices list` for a device
you do not recognise.

**A device:** `roomos devices revoke <id>`. Its sessions stop working on the
next request.

**The auth database or server secret:** remove the auth volume and re-pair every
device. Digests are useless without the secret, but if both leaked together,
treat all sessions as compromised.

Rotate first, investigate second.
