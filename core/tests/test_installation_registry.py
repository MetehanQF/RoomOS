"""The installation registry is loaded once at import, so each case runs in a fresh interpreter."""

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

CORE = Path(__file__).resolve().parents[1]
EXAMPLE = CORE / "app" / "installation.example.yaml"


def _import_catalog(path: Path | None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "ROOMOS_INSTALLATION_PATH"}
    if path is not None:
        env["ROOMOS_INSTALLATION_PATH"] = str(path)
    code = (
        "import json, app.catalog as c; print(json.dumps({"
        "'lights': {l.id: l.entity_id for l in c.LIGHTS}, 'names': c.NODE_NAMES, "
        "'metric': c.NODE_METRIC_BY_ENTITY.get('sensor.my_server_load'), "
        "'camera': c.CAMERA_IMAGE_ENTITY, 'person': c.PRESENCE_ENTITY_ID}))"
    )
    return subprocess.run([sys.executable, "-c", code], cwd=CORE, env=env, capture_output=True, text=True)


def _custom(tmp_path: Path, mutate) -> Path:
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    mutate(data)
    path = tmp_path / "installation.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_bundled_example_is_used_when_unset():
    result = _import_catalog(None)
    assert result.returncode == 0, result.stderr
    loaded = json.loads(result.stdout)
    assert loaded["lights"]["room_main"] == "light.living_room"
    assert "bundled example installation registry" in result.stderr


def test_custom_registry_maps_entities_and_names(tmp_path):
    def mutate(data):
        data["lights"]["room_main"]["entity_id"] = "light.reading_lamp"
        data["nodes"]["primary"]["name"] = "NAS"
        data["nodes"]["primary"]["entities"]["cpu"] = "sensor.my_server_load"
        data["camera"]["image"] = "image.front_door_person"
        data["presence"]["person"] = "person.someone"

    result = _import_catalog(_custom(tmp_path, mutate))
    assert result.returncode == 0, result.stderr
    loaded = json.loads(result.stdout)
    assert loaded["lights"]["room_main"] == "light.reading_lamp"
    assert loaded["names"]["primary"] == "NAS"
    # The API metric id is logical, whatever the entity is called.
    assert loaded["metric"] == ["primary", "primary_cpu"]
    assert loaded["camera"] == "image.front_door_person"
    assert loaded["person"] == "person.someone"
    assert "bundled example" not in result.stderr


def test_missing_required_role_refuses_to_start(tmp_path):
    result = _import_catalog(_custom(tmp_path, lambda d: d["nodes"]["secondary"]["entities"].pop("online")))
    assert result.returncode != 0
    assert "nodes.secondary is missing ['online']" in result.stderr


def test_wrong_entity_domain_is_rejected(tmp_path):
    result = _import_catalog(_custom(tmp_path, lambda d: d["lights"]["room_2"].update(entity_id="switch.bedroom")))
    assert result.returncode != 0
    assert "lights.room_2.entity_id must be a light entity id" in result.stderr


def test_duplicate_light_entity_is_rejected(tmp_path):
    result = _import_catalog(_custom(tmp_path, lambda d: d["lights"]["room_3"].update(entity_id="light.living_room")))
    assert result.returncode != 0
    assert "two lights map to the same entity" in result.stderr


def test_unreadable_registry_is_an_explicit_error(tmp_path):
    result = _import_catalog(tmp_path / "missing.yaml")
    assert result.returncode != 0
    assert "installation registry not readable" in result.stderr
