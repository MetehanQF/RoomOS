# RoomOS Wear OS client (planned)

Not implemented in v0.1.0. This records the intended architecture.

## Approach

Kotlin with Compose for Wear OS, plus Wear OS Tiles.

The watch is a quick-action companion, not a shrunken tablet UI. Fitting a full
control surface onto a watch produces something worse than both. It should offer
the few actions worth reaching for without unlocking a phone, and otherwise stay
out of the way.

## Tile architecture

- A Tile presents a small number of high-value actions plus recent status.
- Tile actions invoke the watch app or service, which calls **RoomOS Core** —
  never Home Assistant directly.
- Which actions appear should come from Core's device and capability response,
  not a list hardcoded into the watch build.
- REST handles commands and snapshots; the RoomOS event WebSocket refreshes
  foreground state.
- Tiles refresh conservatively to preserve battery, and show explicit stale or
  unknown indicators rather than a plausible-looking cached value. On a
  glanceable surface a stale value is more misleading than a blank one.

## Registration

The authenticated phone client mediates watch pairing and revocation. A session
must never be copied by hand to a watch.

The watch should appear as its own device in `roomos devices list`, so losing a
watch is a single revocation and does not affect the phone.

See [../docs/authentication.md](../docs/authentication.md).
