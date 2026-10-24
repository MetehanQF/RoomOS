# Panel Mode acceptance (isolated Chromium)

End-to-end acceptance for the phone Panel Mode against candidate images. Nothing here talks
to production: Home Assistant is a synthetic service (`fakeha/`), sessions are created in a
throwaway auth volume, and the stack runs on a Docker `--internal` network without egress.
No real light, scene or phone is touched.

## Run

```bash
cd tools/panel-accept        # working directory for every command below
npm ci                       # installs the pinned puppeteer-core (no browser download)
npm run accept:panel         # tests roomos-{core,web}:sha-<current HEAD>
npm run accept:panel -- --tag sha-1234567 --label my-run
```

Diagnostics (not gates): `npm run probe:system` (logs every GET/WS frame on `/system` for 90 s)
and `npm run probe:heap` (heap snapshot + detached-DOM retainer path).

Exit code `0` = all checks passed and cleanup was complete, `1` = a check failed,
`2` = cleanup left something behind.

## Requirements

- Docker access, `python3`, `openssl`, `ss` and Node.js ≥ 20.
- Candidate images `roomos-core:<tag>` and `roomos-web:<tag>` built with
  `scripts/build-release-images.sh` (the tag defaults to `sha-<git HEAD short>`).
- A Chromium executable: set `ROOMOS_ACCEPT_CHROME`, otherwise the newest
  `~/.cache/cc-v2-browser/chromium-*/chrome-linux/chrome` or a system Chromium is used.
- First run only: network access to install the pinned `pytest`/`pytest-asyncio` into
  `${XDG_CACHE_HOME:-~/.cache}/roomos-panel-accept/` (fixture builders in `core/tests` import pytest).

Optional: `ROOMOS_ACCEPT_PORT` (loopback forwarder, default `5551`),
`ROOMOS_ACCEPT_OUT` (results directory, default
`${XDG_STATE_HOME:-~/.local/state}/roomos-panel-accept/<label>/`).

## What runs

`run.mjs` → `stack.py up` (fake HA, Core with `fixtures/harness.py` and seeded routines/scenes,
Web with a disposable TLS certificate, loopback forwarder) → `panel-accept.mjs` → `stack.py down`
in a `finally` block. Teardown removes the containers, network, auth volume, forwarder, session
tokens and TLS key, then verifies nothing with the `roomos-accept-` prefix remains.

Results: `<out>/<label>.json` (checks, metrics, cleanup) and screenshots in `<out>/shots/`.
Session tokens are synthetic, exist only for the run and are deleted with the volume.
