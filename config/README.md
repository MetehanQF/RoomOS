# config/

Put your own RoomOS installation registry here. This directory is where
deployment-specific configuration lives, and everything in it except this README
is git-ignored on purpose: a registry describes your home, not the project.

```bash
cp core/app/installation.example.yaml config/installation.yaml
$EDITOR config/installation.yaml
echo "ROOMOS_INSTALLATION_HOST_PATH=$PWD/config/installation.yaml" >> .env
```

Compose mounts that file read-only into Core. With
`ROOMOS_INSTALLATION_HOST_PATH` unset it mounts the bundled example, which maps
entities that do not exist: a fresh checkout starts and serves, and controls
nothing. Core logs a warning while the example is in use.

The schema is documented in [../docs/configuration.md](../docs/configuration.md).
