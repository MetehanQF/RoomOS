from test_auth import auth_client  # noqa: F401
from test_device_center import _session


def test_history_auth_scope_and_catalog(auth_client):
    client, ha, store, _ = auth_client
    endpoint = "/api/v1/devices/primary/history?metric=cpu_usage&range=1h"
    assert client.get(endpoint).status_code == 401
    _session(store, client, ("lights:read",), "No history")
    assert client.get(endpoint).status_code == 403
    _session(store, client, ("devices:read",), "Phones only")
    assert client.get(endpoint).status_code == 404
    _session(store, client, ("system:read",), "Servers")
    assert client.get(endpoint).status_code == 200
    assert client.get(endpoint.replace("primary", "workstation")).status_code == 404
    assert client.get(endpoint.replace("1h", "365d")).status_code == 422
    assert client.get(endpoint.replace("cpu_usage", "battery_percent")).status_code == 422
    assert not ha.calls


def test_forecast_auth_and_metric_catalog(auth_client):
    client, ha, store, _ = auth_client
    endpoint = "/api/v1/devices/primary/forecast"
    assert client.get(endpoint).status_code == 401
    _session(store, client, ("lights:read",), "No forecast")
    assert client.get(endpoint).status_code == 403
    _session(store, client, ("system:read",), "Servers")
    response = client.get(endpoint)
    assert response.status_code == 200 and response.json()["status"] == "insufficient"
    assert client.get(endpoint + "?metric=cpu_usage").status_code == 422
    assert client.get(endpoint.replace("primary", "workstation")).status_code == 404
    assert not ha.calls
