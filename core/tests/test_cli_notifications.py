import json
import os
import sys
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app import cli
from app.fcm import DestinationFile, FcmTargetFile, FcmTargetMetadata
from app.notifications import NotificationDestination
from test_production_notifications import snapshot


class SettingsFixture:
    fcm_enabled = True
    fcm_credentials_path = "/private/credentials.json"
    notification_destinations_path = "/private/destinations.json"
    fcm_targets_path = "/private/targets.json"
    notifications_provider_timeout_seconds = 5
    notifications_path = "/private/notifications.json"
    notifications_queue_limit = 200
    notifications_max_attempts = 3
    notifications_retry_base_seconds = 30


class RuntimeFixture:
    status = "configured"
    providers = (SimpleNamespace(id="fcm"),)
    destinations = (SimpleNamespace(id="handset"),)

    async def close(self):
        return None


def test_privileged_canary_cli_accepts_only_destination(monkeypatch, capsys):
    seen = {}

    class EngineFixture:
        def __init__(self, path, **kwargs):
            seen["path"] = path
            seen["kwargs"] = kwargs

        async def create_canary(self, destination_id):
            seen["destination_id"] = destination_id
            return SimpleNamespace(id="ntf_" + "a" * 32)

    monkeypatch.setattr(cli, "Settings", SettingsFixture)
    monkeypatch.setattr(cli, "load_fcm_runtime", lambda **_kwargs: RuntimeFixture())
    monkeypatch.setattr(cli, "NotificationEngine", EngineFixture)
    monkeypatch.setattr(
        sys, "argv", ["roomos", "notifications", "enqueue-canary", "handset"]
    )

    assert cli.main() == 0
    assert seen["destination_id"] == "handset"
    assert json.loads(capsys.readouterr().out) == {
        "destination_id": "handset",
        "notification_id": "ntf_" + "a" * 32,
        "status": "enqueued",
    }


def test_canary_cli_rejects_arbitrary_payload_arguments(monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "roomos",
        "notifications",
        "enqueue-canary",
        "handset",
        "arbitrary title",
    ])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


def test_activate_production_cli_sets_one_shared_cutoff_without_emitting(
    monkeypatch, capsys, tmp_path
):
    destination_id = "fcm-handset"
    target_ref = "android-handset"
    destinations_path = tmp_path / "destinations.json"
    targets_path = tmp_path / "targets.json"
    incidents_path = tmp_path / "incidents.json"
    destinations_path.write_text(
        DestinationFile(
            destinations=[
                NotificationDestination(
                    id=destination_id,
                    display_name="Phone notifications",
                    enabled=False,
                    activated_at=datetime(2026, 10, 5, 10, 0, tzinfo=UTC),
                    provider="fcm",
                    provider_target_ref=target_ref,
                )
            ]
        ).model_dump_json(exclude_none=True)
    )
    targets_path.write_text(
        FcmTargetFile(
            targets={target_ref: "synthetic-registration-token"},
            registrations={
                target_ref: FcmTargetMetadata(
                    device_id="device-handset",
                    registered_at=datetime(2026, 10, 5, 10, 0, tzinfo=UTC),
                    token_fingerprint="a" * 64,
                )
            },
        ).model_dump_json(exclude_none=True)
    )
    os.chmod(destinations_path, 0o600)
    os.chmod(targets_path, 0o600)

    settings = SimpleNamespace(
        fcm_enabled=True,
        fcm_credentials_path="/private/credentials.json",
        notification_destinations_path=str(destinations_path),
        fcm_targets_path=str(targets_path),
        notifications_provider_timeout_seconds=5,
        notification_incidents_path=str(incidents_path),
        ha_connect_enabled=True,
        has_ha_credentials=True,
    )

    class ClientFixture:
        def __init__(self, _settings):
            pass

        async def close(self):
            pass

    class DnsFixture:
        def __init__(self, _settings, _publish):
            pass

        async def probe_now(self):
            pass

        def snapshot(self):
            return None

    async def healthy_overview(_client, _dns):
        return snapshot()

    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "load_fcm_runtime", lambda **_kwargs: RuntimeFixture())
    monkeypatch.setattr(cli, "HomeAssistantClient", ClientFixture)
    monkeypatch.setattr(cli, "DnsHealthMonitor", DnsFixture)
    monkeypatch.setattr(cli, "system_overview", healthy_overview)
    monkeypatch.setattr(
        sys,
        "argv",
        ["roomos", "notifications", "activate-production", destination_id],
    )

    assert cli.main() == 0
    result = json.loads(capsys.readouterr().out)
    updated = DestinationFile.model_validate_json(destinations_path.read_text())
    state = json.loads(incidents_path.read_text())
    destination = updated.destinations[0]
    assert result["status"] == "activated"
    assert destination.enabled is True
    assert set(destination.severity_policy) == {"info", "warning", "critical"}
    assert datetime.fromisoformat(result["rollout_epoch"]) == destination.activated_at
    assert state["rollout_epoch"].replace("Z", "+00:00") == result[
        "rollout_epoch"
    ].replace("Z", "+00:00")
    assert all(value["phase"] == "healthy" for value in state["incidents"].values())
    assert incidents_path.stat().st_mode & 0o777 == 0o600
    assert "synthetic-registration-token" not in json.dumps(result)
