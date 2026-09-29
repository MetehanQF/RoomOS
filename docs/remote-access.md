# Remote access

RoomOS has no public ingress and does not need one. This page describes the
supported way to reach it from outside the house.

> **Do not port-forward RoomOS, publish a public DNS A record for it, or put it
> behind a public tunnel.** Device authentication raises the bar, but the design
> assumes a private network boundary. See [../SECURITY.md](../SECURITY.md).

## The shape of the problem

A tablet at home should reach RoomOS. The same phone should reach it from
elsewhere. Ideally both use **the same URL**, because:

- The PWA has one origin, so one installed app works in both places.
- Session cookies are pinned to an origin; two names means two sessions.
- `ROOMOS_AUTH_ALLOWED_ORIGIN` is exact, and it is one value.

So the goal is one hostname that resolves to a private address, wherever you are.

## Recommended: private overlay network with split DNS

Use a mesh VPN — Tailscale, WireGuard, Nebula — plus DNS that answers the
RoomOS name with a private address.

```text
          roomos.example.com
                  │
     ┌────────────┴────────────┐
     │   private/split DNS     │   answers only inside the overlay or LAN
     └────────────┬────────────┘
                  │
          private RoomOS host
     (overlay address or LAN address)
```

Public DNS has no record for the name. Outside the overlay it does not resolve,
so there is nothing to scan or reach.

### Subnet routing vs a host route

Two ways to make the host reachable over the overlay:

- **Advertise the whole LAN subnet** from one node. Simple, but every overlay
  client can now reach every device on your LAN, including the ones with no
  authentication at all.
- **Advertise a single `/32` host route** for the RoomOS host. Slightly more
  setup, and the overlay grants reachability to exactly one address.

Prefer the `/32` route. Remote access to a light switch is not a reason to put
your printer and your cameras on the VPN.

### Least-privilege ACLs

Whatever overlay you use, express the access rule as narrowly as it supports:
which devices may reach which address, on which port. Default-allow meshes are
convenient and defeat the point of the host route above.

Review the rule when you add a device. An overlay ACL is the kind of thing that
quietly accumulates.

### No funnel or public relay

Overlay products often offer a feature that publishes a service to the public
internet (Tailscale calls it Funnel). **Do not use it for RoomOS.** It converts
a private service into a public one, which is exactly what the rest of this
design avoids.

## TLS and the certificate problem

RoomOS session cookies are `Secure`, and a PWA service worker needs a secure
origin. So you need a certificate for a name that resolves to a private address
— which a public CA will not validate by HTTP challenge, because the address is
not reachable from the internet.

Two workable routes:

**DNS-01 with a domain you control.** Any ACME client that supports DNS-01 can
issue for `roomos.example.com` without the host being publicly reachable; the
challenge is a TXT record. Then resolve that name to the private address on your
own resolver. The certificate is publicly trusted, the name only resolves
privately.

One consequence to accept knowingly: any publicly trusted certificate is logged
in **Certificate Transparency**, so the hostname becomes publicly enumerable
even though it resolves nowhere public. Choose a name you do not mind appearing
in a CT log, and do not treat the name as a secret.

**Your own internal CA.** Issue the certificate yourself and install the root on
each client device. No CT exposure, more per-device setup, and you own the
renewal story.

Either way, RoomOS only ever receives a PEM pair through the optional TLS
overlay — it does not issue or renew certificates. See
[installation.md](installation.md#8-optional-tls).

## Configuration

Whichever route you take, the name must be consistent:

```bash
# .env
ROOMOS_BIND_ADDR=<private address of this host>
ROOMOS_AUTH_ALLOWED_ORIGIN=https://roomos.example.com
ROOMOS_AUTH_ALLOWED_HOST=roomos.example.com
ROOMOS_TLS_CERT=/absolute/path/to/fullchain.pem
ROOMOS_TLS_KEY=/absolute/path/to/privkey.pem
```

```bash
docker compose -f compose.yaml -f compose.tls.yaml up -d
```

If the name your clients type does not match the two auth variables exactly,
every authenticated request is refused. That is the check working — fix the
values rather than loosening them.

## Verifying

- The name resolves to the private address from inside the overlay or LAN.
- The name does **not** resolve from a public resolver.
- RoomOS is listening on the address you configured, not `0.0.0.0`:
  `ss -tlnp | grep 5500`
- Reaching the host over the overlay serves the same origin as at home, and an
  existing session still works.
