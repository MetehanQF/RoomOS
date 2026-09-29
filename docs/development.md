# Development

No Home Assistant instance, token, or paired device is required. The suite runs
entirely against fakes, and the bundled device registry maps entities that do
not exist.

## Layout

```text
core/          FastAPI service
  app/         catalog (registry) · policy · auth · service · events · main · cli
  tests/       api · auth · events · production policy
web/           React + TypeScript + Vite PWA
  src/         pages · components · api · auth · hooks · ui
shared/api/    client contract: JSON Schemas + notes
docker/        nginx configuration
config/        your device registry (git-ignored)
docs/          this documentation
```

## Setup

```bash
# Core
cd core
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest

# Web
cd ../web
npm ci
npm test
npm run lint
npm run build

# Compose
cd ..
docker compose config
docker compose build
```

`docker compose build` only builds images. It does not start containers, contact
Home Assistant, publish a port, or touch an existing network.

Run all of it before opening a pull request — it is the same set CI runs.

## Running locally

Against fakes, with authentication off:

```bash
# .env
ROOMOS_AUTH_ENABLED=false
ROOMOS_HA_CONNECT_ENABLED=false
```

```bash
cd core && .venv/bin/uvicorn app.main:app --port 5501 --reload
cd web && npm run dev
```

Core serves `/api/v1/health` and reports `not_configured`. Never run a real
deployment with `ROOMOS_AUTH_ENABLED=false`.

To exercise authentication locally you need a secure origin, because session
cookies are `Secure`. Use the TLS overlay, or a local reverse proxy that
terminates TLS, and set `ROOMOS_AUTH_ALLOWED_ORIGIN` / `_HOST` to match exactly.

## Testing

Core tests use a `FakeHA` fixture recording every service call, so a test can
assert that an invalid request produced **zero** calls — the property that
matters for a system that turns real things on and off.

Tests derive expectations from the active registry rather than hardcoding device
IDs. If you add a fixture device, do it in the registry, not inline.

Web tests use Vitest. `api.test.ts` covers the runtime shape check on the
`/system` response, so a Core/Web contract drift fails before React renders.

## The two invariants

Both are covered by tests. Keep them that way:

1. **No Home Assistant entity ID ever reaches a client** — not in a response
   body, an error detail, a log line, or an event payload.
2. **No token or session secret is ever logged or returned.**

If you change `catalog.py`, `policy.py`, `auth.py`, `ha_client.py` or `audit.py`,
re-read those tests before assuming your change is safe.

## Security-sensitive files

| File | Why |
| --- | --- |
| `core/app/auth.py` | Pairing, sessions, CSRF, revocation |
| `core/app/policy.py` | The permission matrix |
| `core/app/catalog.py` | The logical-ID boundary |
| `core/app/ha_client.py` | Where the HA token is used |
| `core/app/audit.py` | Log sanitisation |
| `docker/`, `compose*.yaml` | Container hardening and exposure |

## House rules

**Nothing deployment-specific in tracked source.** No real addresses,
hostnames, entity IDs, paths, or names. CI runs a privacy scan that fails the
build on these patterns; it is a safety net, not a substitute for looking. See
[../CONTRIBUTING.md](../CONTRIBUTING.md).

**Anything site-specific belongs in the registry or an environment variable.**
If you add a field, add it to `core/app/devices.example.yaml` with a placeholder
and document it in [configuration.md](configuration.md).

**Never commit auth state.** `data/`, `*.db`, `server-secret` are git-ignored.
If you generate a database while testing, it stays local.

## Commit messages

Conventional Commits:

```text
feat(core): add per-device effect allowlist
fix(web): keep stale metrics visible during reconnect
docs: clarify the read-only default
test(core): cover kill switch over scoped policy
ci: run the privacy scan on pull requests
```

Scopes in use: `core`, `web`, `auth`, `docker`, `docs`, `ci`.

## CI

`.github/workflows/ci.yml` runs on push and pull request:

| Job | What |
| --- | --- |
| Privacy scan | Denylist over tracked files |
| Core tests | `pytest` |
| Web | `vitest`, ESLint, production build |
| Compose | Config validation, nginx syntax check, image build |

No secrets, no Home Assistant instance, no network services.
