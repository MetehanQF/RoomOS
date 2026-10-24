# RoomOS Android client

Native read-only RoomOS client for Android phones and tablets.

- Package: `com.metehantech.roomos`
- Minimum SDK: 26 (Android 8.0)
- Target/compile SDK: 36 (Android 16)
- UI: Kotlin + Jetpack Compose + Material 3
- Network boundary: only your RoomOS origin and its WSS endpoint (see *Your server* below)
- Session: revocable RoomOS native bearer session encrypted with a non-exportable
  Android Keystore AES/GCM key; ciphertext is kept in `noBackupFilesDir`
- Backup: disabled; session material is never migrated to another device
- Notifications: Firebase Messaging registration is bound to the existing
  native RoomOS device session; warning and critical messages use separate
  Android channels

The client has no light/routine write actions. Native pairing uses an
operator-created `android-readonly` one-time pairing window and receives only
`lights:read`, `system:read`, `camera:read`, `activity:read`, and
`routines:read`. The FCM token is sent only to the fixed RoomOS registration
endpoint over that bearer session; it is never rendered or logged. Declining
Android notification permission does not block the rest of the app.

## Your server

The app talks to exactly one origin, fixed at build time. Before building, set
your host in both places:

- `app/src/main/java/com/metehantech/roomos/network/RoomOsApiClient.kt` —
  `PRODUCTION_ORIGIN`
- `app/src/main/res/xml/network_security_config.xml` — the `<domain>` entry

It must match `ROOMOS_AUTH_ALLOWED_ORIGIN` on the server exactly.

## Building

Build tooling is intentionally not part of production Compose. Use JDK 17,
Gradle 8.13, Android SDK 36, and:

```bash
./gradlew testInternalUnitTest lintInternal assembleInternal
```

Internal APK output: `app/build/outputs/apk/internal/app-internal.apk`.

Push needs your own Firebase project: place its `google-services.json` in `app/`
(git-ignored). The Gradle build refuses to run without that file; to build and
test without push, a placeholder project file for the package names
`com.metehantech.roomos` and `com.metehantech.roomos.internal` is enough. Keep it and any signing material outside Git. The internal build
uses the `.internal` application ID suffix; sign release builds with your own
key.
