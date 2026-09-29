# config/

Put your own RoomOS device registry here. This directory is where deployment
specific configuration lives, and its contents are git-ignored on purpose:
a registry describes your home, not the project.

```bash
cp core/app/devices.example.yaml config/devices.yaml
$EDITOR config/devices.yaml
echo "ROOMOS_DEVICE_REGISTRY=$PWD/config/devices.yaml" >> .env
```

With `ROOMOS_DEVICE_REGISTRY` unset, RoomOS falls back to the bundled example
registry, which maps entities that do not exist. That makes a fresh checkout
safe but inert: it starts and serves, and controls nothing.

The schema is documented in [../docs/configuration.md](../docs/configuration.md).
