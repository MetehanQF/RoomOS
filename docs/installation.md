# Installing RoomOS

This walks a first deployment in the order that keeps the blast radius small:
authenticate first, read second, control last.

> RoomOS is designed for a trusted local network or a private overlay. Do not
> port-forward it or publish a public DNS record. See
> [remote-access.md](remote-access.md) and [../SECURITY.md](../SECURITY.md).

## Requirements

- Docker with Compose v2 (`docker compose version`)
- A Home Assistant instance reachable from this host
- A host on the same trusted network as your tablet

## 1. Get the source

```bash
git clone https://github.com/MetehanQF/RoomOS.git
cd RoomOS
cp .env.example .env
chmod 600 .env
```

`.env` is git-ignored. Keep it that way.

## 2. Create a dedicated Home Assistant user

Do not use your own account. A long-lived token inherits its user's full
permissions and Home Assistant has no per-entity token scoping, so the account
is the coarse boundary and RoomOS's registry is the fine one.

1. **Settings → People → Add person**. Give it login access, name it `RoomOS`,
   no administrator rights.
2. Sign in as that user in a private window.
3. Profile → **Security** → **Long-lived access tokens** → **Create token**.
4. Copy it once into `.env` as `HA_TOKEN=...`.

If the token is ever shown somewhere it should not be, revoke it in Home
Assistant and create another. Revocation is what ends the exposure; editing
files does not.

## 3. Point RoomOS at Home Assistant

```bash
HA_URL=http://homeassistant.local:8123
HA_TOKEN=<the token you just created>
```

Use whatever address Home Assistant is actually reachable at from this host.

## 4. Set the name your clients will use

RoomOS pins an exact host and origin for authenticated requests.

```bash
ROOMOS_AUTH_ALLOWED_ORIGIN=https://roomos.example.com
ROOMOS_AUTH_ALLOWED_HOST=roomos.example.com
```

Both must match what clients actually type. Session cookies are `Secure`, so a
real deployment needs HTTPS — see [step 8](#8-optional-tls).

For a first local run before TLS exists, you can develop against
`ROOMOS_AUTH_ENABLED=false`. Never leave a real deployment that way.

## 5. Describe your installation

RoomOS refuses to touch anything it does not know about.

```bash
cp core/app/installation.example.yaml config/installation.yaml
$EDITOR config/installation.yaml
echo "ROOMOS_INSTALLATION_HOST_PATH=$PWD/config/installation.yaml" >> .env
```

Replace the placeholder entities with your own: lights, the two monitored hosts,
the camera and the presence entities. Find them in Home Assistant under
**Developer tools → States**. Schema:
[configuration.md](configuration.md#installation-registry).

`config/installation.yaml` is git-ignored — it describes your house. A missing
or malformed entry stops Core at boot with a message naming the key.

## 6. First start

Leave `ROOMOS_CONTROL_ENABLED=false` and enable the live read connection:

```bash
ROOMOS_HA_CONNECT_ENABLED=true
```

```bash
docker compose build          # images are tagged with ROOMOS_IMAGE_TAG from .env
docker compose up -d
docker compose ps
```

Both services should report healthy. Compose refuses to start without
`ROOMOS_IMAGE_TAG`, so a deployment always names the exact build it runs; for
immutable `sha-<commit>` tags use `scripts/build-release-images.sh`.

## 7. Pair your first device

Pairing is closed until you open a window:

```bash
docker compose exec core roomos pairing create
```

Open RoomOS on the device, enter the code, and name it. The code is single-use
and expires; if it lapses, create another.

Confirm what the session can do:

```bash
curl -s http://127.0.0.1:5500/api/v1/health
```

Expect `"home_assistant": "connected"`, `"control_enabled": false`, and empty
`writable_device_ids`. Your lights should appear in the UI with real state, and
every control visibly locked.

If a device shows as unavailable, its entity ID in the registry does not match
Home Assistant. Fix the registry rather than widening anything.

Device management: [authentication.md](authentication.md#managing-devices).

## 8. Optional TLS

Session cookies are `Secure` and installing the PWA needs a secure origin, so
any real deployment wants this.

RoomOS does not issue certificates. Obtain a PEM pair — see
[remote-access.md](remote-access.md#tls-and-the-certificate-problem) for the two
practical routes — then:

```bash
ROOMOS_TLS_CERT=/absolute/path/to/fullchain.pem
ROOMOS_TLS_KEY=/absolute/path/to/privkey.pem
ROOMOS_HTTPS_PORT=8443
```

```bash
docker compose -f compose.yaml -f compose.tls.yaml up -d
```

HTTP now returns a 308 redirect to HTTPS, preserving path and query. Confirm no
certificate warning, then use the browser's **Install app**.

Renewal is yours to schedule. After the PEM files change, recreate the web
container so nginx reloads them:

```bash
docker compose -f compose.yaml -f compose.tls.yaml up -d --no-deps --force-recreate web
```

## 9. Reach it from a tablet

```bash
ROOMOS_BIND_ADDR=192.0.2.10    # this host's actual private address
```

```bash
docker compose up -d --no-deps --force-recreate web
ss -tlnp | grep 5500
```

You should see the specific address, not `0.0.0.0`. If you see `0.0.0.0`, stop
and fix `ROOMOS_BIND_ADDR` before continuing.

## 10. Enable control

Only after reads have been correct for a while. Start with one device and one
action:

```bash
ROOMOS_CONTROL_ENABLED=true
ROOMOS_CONTROL_POLICY=scoped
ROOMOS_CONTROL_DEVICE_IDS=desk_ambient
ROOMOS_CONTROL_ACTIONS=power
```

```bash
docker compose up -d --no-deps --force-recreate core
curl -s http://127.0.0.1:5500/api/v1/health | grep writable
```

Verify the permitted action works and a neighbouring one is refused with 403.
Then widen, or switch to `ROOMOS_CONTROL_POLICY=production` for the full matrix.

To return to read-only at any time, set `ROOMOS_CONTROL_ENABLED=false` and
recreate Core. The kill switch overrides the policy, so you do not need to
unwind the scope lists.

## Updating

```bash
git pull
docker compose build
docker compose up -d
```

Read [../CHANGELOG.md](../CHANGELOG.md) first while the major version is `0`:
minor releases may break configuration.

## Uninstalling

```bash
docker compose down
docker volume rm roomos_roomos_auth      # revokes every paired device
docker image rm roomos-core:<tag> roomos-web:<tag>
```

Then revoke the Home Assistant token and delete the dedicated HA user.

## Troubleshooting

**`"home_assistant": "not_configured"`** — `HA_TOKEN` is empty or did not reach
the container. Check `.env`, then `docker compose config`.

**`"home_assistant": "disconnected"`** — Core cannot reach `HA_URL`, or the
token was rejected. Test reachability from inside the container; if that works,
create a fresh token.

**Every request returns 403 after pairing** — the `Host` or `Origin` does not
match `ROOMOS_AUTH_ALLOWED_HOST` / `ROOMOS_AUTH_ALLOWED_ORIGIN` exactly. This is
the most common misconfiguration. Compare them with what the browser actually
sends.

**Pairing code rejected** — it expired, was already used, or the rate limit
tripped. Create another; wait out the window if you burned the attempts.

**Session lost after restart** — the auth volume was removed. Device records and
the server secret live in `roomos_auth`.

**Devices show unavailable** — registry entity IDs do not match Home Assistant,
or Core is still on the bundled example (it logs a warning at startup).

**Controls stay locked** — expected unless `ROOMOS_CONTROL_ENABLED=true` *and*
the policy grants that device and action. `/api/v1/health` shows exactly what is
writable; trust it over the UI.

**422 on colour or effects** — the device is not advertising that capability
right now. Some bulbs only report an effect list while powered on.
