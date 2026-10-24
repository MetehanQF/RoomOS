import json
import sqlite3

from app.auth import CSRF_COOKIE, SESSION_COOKIE
from app.panel_preferences import DEFAULT_PANEL_LAYOUT

from test_auth import auth_client, csrf_headers, pair  # noqa: F401


def test_panel_layout_is_device_bound_and_survives_reinitialize(auth_client):
    client, _, store, settings = auth_client
    first = pair(client, store, "Phone 2 panel")
    first_id = first.json()["device"]["id"]
    changed = DEFAULT_PANEL_LAYOUT.model_copy(
        update={"density": "compact", "cards": ["ambient", "main_lights", "scenes", "routines", "system"]}
    )
    store.save_panel_layout(first_id, changed)

    second = pair(client, store, "Tab")
    second_id = second.json()["device"]["id"]
    assert store.panel_layout(first_id)[0] == changed
    assert store.panel_layout(second_id) is None

    store.initialize()
    assert store.panel_layout(first_id)[0] == changed
    with sqlite3.connect(settings.auth_database_path) as db:
        assert db.execute("SELECT count(*) FROM panel_preferences").fetchone()[0] == 1


def test_panel_layout_api_defaults_saves_and_resets_without_ha_or_new_scope(auth_client):
    client, fake_ha, store, _ = auth_client
    paired = pair(client, store, "Phone 2 panel")
    assert "lights:write" in paired.json()["device"]["scopes"]  # unchanged pairing profile

    default = client.get("/api/v1/panel/layout")
    assert default.status_code == 200
    assert default.headers["cache-control"].startswith("no-store")
    assert default.json()["source"] == "default"

    layout = default.json()["layout"] | {
        "density": "compact",
        "hidden": ["routines"],
        "header": {"clock": True, "date": False, "connection": True, "health": False},
    }
    saved = client.put("/api/v1/panel/layout", json=layout, headers=csrf_headers(client))
    assert saved.status_code == 200
    assert saved.json()["source"] == "saved"
    assert saved.json()["layout"] == layout
    assert client.get("/api/v1/panel/layout").json()["layout"] == layout
    assert fake_ha.calls == []

    reset = client.delete("/api/v1/panel/layout", headers=csrf_headers(client))
    assert reset.status_code == 200
    assert reset.json()["source"] == "default"
    assert client.get("/api/v1/panel/layout").json()["layout"] == DEFAULT_PANEL_LAYOUT.model_dump(mode="json")


def test_panel_layout_writes_require_auth_origin_and_csrf(auth_client):
    client, _, store, _ = auth_client
    pair(client, store, "Phone 2 panel")
    body = DEFAULT_PANEL_LAYOUT.model_dump(mode="json")
    assert client.put("/api/v1/panel/layout", json=body).status_code == 403
    assert client.put(
        "/api/v1/panel/layout",
        json=body,
        headers={"Origin": "https://evil.example", "X-CSRF-Token": client.cookies.get(CSRF_COOKIE)},
    ).status_code == 403
    client.cookies.delete(SESSION_COOKIE)
    assert client.get("/api/v1/panel/layout").status_code == 401


def test_panel_layout_rejects_unknown_duplicate_and_dangerous_values(auth_client):
    client, _, store, _ = auth_client
    pair(client, store, "Phone 2 panel")
    base = DEFAULT_PANEL_LAYOUT.model_dump(mode="json")
    cases = [
        base | {"cards": ["main_lights", "main_lights", "scenes", "routines", "system"]},
        base | {"cards": ["light.living_room", "ambient", "scenes", "routines", "system"]},
        base | {"hidden": ["main_lights", "ambient"]},
        base | {"scene_order": base["scene_order"][:-1]},
        base | {"extra": "forbidden"},
    ]
    for body in cases:
        response = client.put("/api/v1/panel/layout", json=body, headers=csrf_headers(client))
        assert response.status_code == 422, json.dumps(body)


def test_corrupt_persisted_layout_falls_back_without_deleting_row(auth_client):
    client, _, store, settings = auth_client
    paired = pair(client, store, "Phone 2 panel")
    device_id = paired.json()["device"]["id"]
    with sqlite3.connect(settings.auth_database_path) as db:
        db.execute(
            "INSERT INTO panel_preferences(device_id,version,layout,updated_at) VALUES(?,?,?,?)",
            (device_id, 1, '{"cards":["evil"]}', 123),
        )
    response = client.get("/api/v1/panel/layout")
    assert response.status_code == 200
    assert response.json()["source"] == "default"
    assert response.json()["layout"] == DEFAULT_PANEL_LAYOUT.model_dump(mode="json")
    with sqlite3.connect(settings.auth_database_path) as db:
        assert db.execute("SELECT layout FROM panel_preferences WHERE device_id=?", (device_id,)).fetchone() is not None


def test_revoking_device_cascades_panel_layout(auth_client):
    client, _, store, settings = auth_client
    device_id = pair(client, store, "Phone 2 panel").json()["device"]["id"]
    store.save_panel_layout(device_id, DEFAULT_PANEL_LAYOUT)
    assert store.revoke_device(device_id)
    with sqlite3.connect(settings.auth_database_path) as db:
        assert db.execute("SELECT count(*) FROM panel_preferences WHERE device_id=?", (device_id,)).fetchone()[0] == 0
