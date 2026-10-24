# Contributing to RoomOS

Thanks for taking a look. Issues and pull requests are both welcome.

## The one rule that matters most

**No real deployment details in a contribution.** RoomOS is written against real
houses, so patches naturally pick up real addresses and entity IDs. None of that
belongs in the repository.

Never commit:

- Real LAN, CGNAT, or overlay/VPN IP addresses — use `192.0.2.0/24` (RFC 5737)
  or a name
- Real hostnames, domains, or tailnet names — use `example.com`,
  `roomos.example.com`, or `home.arpa`
- Real Home Assistant entity IDs — use `light.living_room` style placeholders
- Real room, device, or person names
- MAC addresses
- Absolute home paths under `/home` or `/Users` — use `$HOME` or `/srv`
- Tokens, passwords, cookies, session values, pairing codes, private keys or
  certificates
- Auth database files, `server-secret`, or anything from `data/`
- Your own name or email in source files
- Infrastructure specifics: internal hostnames, backup paths, credential file
  locations

CI runs a privacy scan on every push and pull request and will fail the build on
these patterns. It is a safety net, not a substitute for looking.

Your commit author name and email become permanently public. If you would rather
not publish a personal address, enable *Keep my email addresses private* in your
GitHub email settings and commit with your `@users.noreply.github.com` address.

## Development setup

No Home Assistant instance, token, or paired device is required. The suite runs
entirely against fakes, and the bundled registry maps entities that do not
exist.

```bash
# Core
cd core && python3 -m venv .venv && .venv/bin/pip install -e '.[test]' && .venv/bin/pytest

# Web
cd ../web && npm ci && npm test && npm run lint && npm run build

# Compose
cd .. && docker compose config
```

Run all of it before opening a pull request. More in
[docs/development.md](docs/development.md).

## Working on devices and entities

Anything deployment-specific goes in the installation registry, never in source.

If you add a field, add it to `core/app/installation.example.yaml` with a placeholder
and document it in [docs/configuration.md](docs/configuration.md). If a test
needs a device, add it to the example registry — do not hardcode an entity ID or
a device count in a test. Existing tests derive both from the registry; keep it
that way.

## Security-sensitive changes

Extra care when touching:

- `core/app/auth.py` — pairing, sessions, CSRF, revocation
- `core/app/policy.py` — the permission matrix
- `core/app/catalog.py` — the logical-ID boundary
- `core/app/ha_client.py` — where the HA token is used
- `core/app/audit.py` — log sanitisation
- `docker/`, `compose*.yaml` — container hardening and exposure

Two invariants must hold, and both are covered by tests:

1. **No Home Assistant entity ID ever reaches a client.**
2. **No token or session secret is ever logged or returned.**

Never weaken a check to make something convenient. In particular: the `Host` and
`Origin` comparisons are exact on purpose, and pairing is closed by default on
purpose.

If you find a vulnerability, follow [SECURITY.md](SECURITY.md) instead of
opening a public issue.

## Pull requests

- One logical change per PR.
- Explain *why*, not just what. The diff shows what.
- Add a test for behaviour changes, especially around auth, policy and
  validation.
- Match the surrounding style; the codebase is deliberately terse.
- Note anything user-visible so it can go in the changelog.

Do not bump the version or edit `CHANGELOG.md` in a PR — that happens at
release.

## Commit messages

Conventional Commits:

```text
feat(auth): add per-device scope editing
fix(web): keep stale metrics visible during reconnect
docs: clarify the read-only default
test(core): cover kill switch over scoped policy
ci: run the privacy scan on pull requests
```

Scopes in use: `core`, `web`, `auth`, `docker`, `docs`, `ci`.

## Reporting bugs

Include the RoomOS version or commit, how you deployed it, what you expected,
what happened, and relevant log lines.

**Scrub logs and screenshots before posting.** Redact entity IDs, addresses,
hostnames, device names, pairing codes and session cookies. If a screenshot
shows your browser address bar or a real device name, crop or blur it.

## License

Contributions are accepted under the [Apache License 2.0](LICENSE), matching the
project.
