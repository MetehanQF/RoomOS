# RoomOS shared API contract

All clients consume `/api/v1` and use stable RoomOS IDs. Home Assistant entity IDs are an
internal implementation detail and must not appear in client contracts.

- REST discovery: `/api/openapi.json`
- Realtime events: `/api/v1/events`
- Event schema: `event.schema.json` — generated from `EventType` in `core/app/models.py`.
  Regenerate with `python tools/export_event_schema.py` from `core/`; Core and Web parity tests
  fail when the schema, the emitting code or the Web union drift apart.
- Group result schema: `group-command-result.schema.json`

Source-of-truth models live in `core/app/models.py`; the live generated OpenAPI document remains
available at `/api/openapi.json`.
