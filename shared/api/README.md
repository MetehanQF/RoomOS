# RoomOS shared API contract

All clients consume `/api/v1` and use stable RoomOS IDs. Home Assistant entity IDs are an
internal implementation detail and must not appear in client contracts.

- REST discovery: `/api/openapi.json`
- Realtime events: `/api/v1/events`
- Event schema: `event.schema.json`
- Group result schema: `group-command-result.schema.json`

Source-of-truth models live in `core/app/models.py`; the live generated OpenAPI document remains
available at `/api/openapi.json`.

The logical IDs a deployment exposes come from its device registry, so they differ between
installations. Clients should discover them from `/api/v1/lights` and `/api/v1/system` rather than
assuming any particular set, and must treat `/api/v1/system` node keys as registry-defined.

All endpoints except `/api/v1/health` and the pairing routes require an authenticated device
session; see [../../docs/authentication.md](../../docs/authentication.md).
