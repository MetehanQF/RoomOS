"""Synthetic Home Assistant for isolated RoomOS acceptance (no real devices, no network egress).

REST /api/states/<entity> and /api/history, WebSocket auth+subscribe like HA. Sensor values
behave like HA REST sensors: last_updated moves only when the state changes. Scenario control
and counters are exposed only on this test-only service (/_control, /_stats).
"""
import asyncio
import json
import time
from collections import Counter
from datetime import UTC, datetime

from pathlib import Path
from fastapi import FastAPI, Request, Response, WebSocket

app = FastAPI()
START = time.monotonic()
MAPPING=json.loads((Path(__file__).parent/"mapping.json").read_text())
WORKSTATION={}
for key,eid in MAPPING.items():
    # Production-shaped HASS.Agent Storage attributes (7D.2-D): C: is 99% full.
    attrs={"Name":"C","Label":"Windows","FileSystem":"NTFS","TotalSizeMB":510669.0,"AvailableSpaceMB":6373.0,"UsedSpaceMB":504296.0,"AvailableSpacePercentage":1,"UsedSpacePercentage":99} if key=="disk_usage" else {}
    raw={"heartbeat":"1","cpu_usage":"23","ram_usage":"92","gpu_usage":"7.4","battery_percent":"100","charging":"High","ac_connected":"Online","disk_usage":"Windows"}[key]
    WORKSTATION[eid]={"entity_id":eid,"state":raw,"attributes":attrs,"last_updated":datetime.now(UTC).isoformat(),"last_changed":datetime.now(UTC).isoformat()}
SCHEDULE=[("cpu_usage",30),("ram_usage",30),("gpu_usage",30),("battery_percent",60),("charging",60),("disk_usage",300),("heartbeat",30)]
SOURCE_EVENTS=[]
stats = Counter()
scenario = {"mode": "drift", "down": False, "secondary_online": True, "cpu_override": None, "workstation": "live"}
SAVED = {}  # Workstation values kept while the agent is offline (HA restores them on reconnect)
_last: dict[str, tuple[str, str]] = {}  # entity -> (state, last_updated)

BASE = {
    "primary": {"cpu": 22.0, "ram": 61.2, "disk": 36.9, "temperature": 48.5, "uptime": "6d 6h"},
    "secondary": {"cpu": 11.0, "ram": 44.1, "disk": 6.0, "temperature": 41.0, "uptime": "12d 3h"},
}
UNITS = {"cpu": "%", "ram": "%", "disk": "%", "temperature": "°C"}


def _value(entity_id: str) -> tuple[str, dict] | None:
    if entity_id in WORKSTATION:
        v=WORKSTATION[entity_id]; return v["state"],v["attributes"]
    domain, name = entity_id.split(".", 1)
    node, _, suffix = name.partition("_")
    minute = int((time.monotonic() - START) // 60)
    if node in BASE and domain == "sensor" and suffix in BASE[node]:
        value = BASE[node][suffix]
        if node == "primary" and suffix == "cpu" and scenario["cpu_override"] is not None:
            value = scenario["cpu_override"]
        elif scenario["mode"] == "drift" and suffix in {"cpu", "temperature"}:
            value = round(value + (minute % 3) * 0.5, 1)  # real metrics change about once a minute
        return str(value), ({"unit_of_measurement": UNITS[suffix]} if suffix in UNITS else {})
    if node in BASE and domain == "binary_sensor":
        if suffix == "online":
            return ("on" if node == "primary" or scenario["secondary_online"] else "off"), {}
        return ("on" if suffix in {"docker", "tailscale", "cloudflared", "smart", "portainer"} else "off"), {}
    if entity_id == "sensor.primary_nextcloud":
        return "ok", {}
    if entity_id == "sensor.frigate_status":
        return "running", {}
    if entity_id == "sun.sun":
        return "above_horizon", {"next_rising": "2026-10-15T04:10:00+00:00", "next_setting": "2026-10-14T15:25:00+00:00"}
    if entity_id.startswith("light."):
        return "off", {"min_color_temp_kelvin": 2500, "max_color_temp_kelvin": 6500, "effect_list": ["Off"]}
    return None


@app.get("/api/states/{entity_id}")
async def state(entity_id: str):
    stats["state_reads"] += 1
    if entity_id == "sensor.primary_cpu":
        stats["primary_snapshot_reads"] += 1
    if scenario["down"]:
        stats["reads_while_down"] += 1
        return Response(status_code=502)
    found = _value(entity_id)
    if found is None:
        return Response(status_code=404)
    raw, attributes = found
    previous = _last.get(entity_id)
    if previous is None or previous[0] != raw:
        _last[entity_id] = (raw, datetime.now(UTC).isoformat())
    return {"entity_id": entity_id, "state": raw, "attributes": attributes, "last_updated": _last[entity_id][1],
            "last_changed": _last[entity_id][1]}


@app.get("/api/history/period/{start}")
async def history(start: str, request: Request):
    stats["history_reads"] += 1
    ids = request.query_params.get("filter_entity_id", "").split(",")
    return [[] for _ in ids if ids != [""]]


@app.post("/api/services/{domain}/{service}")
async def services(domain: str, service: str):
    stats["service_calls"] += 1  # must stay 0: acceptance never controls devices
    return Response(status_code=403)


@app.websocket("/api/websocket")
async def websocket(socket: WebSocket):
    await socket.accept()
    if scenario["down"]:
        await socket.close(); return
    await socket.send_text(json.dumps({"type": "auth_required", "ha_version": "2026.9.3"}))
    await socket.receive_text()
    await socket.send_text(json.dumps({"type": "auth_ok", "ha_version": "2026.9.3"}))
    message = json.loads(await socket.receive_text())
    await socket.send_text(json.dumps({"id": message.get("id"), "type": "result", "success": True, "result": None}))
    stats["ws_sessions"] += 1
    try:
        while not scenario["down"]:
            await asyncio.sleep(1)
            elapsed=int(time.monotonic()-START)
            mode=scenario["workstation"]
            if mode=="offline" and not SAVED:
                # HASS.Agent LWT: HA marks every agent entity unavailable at once.
                for eid,cur in list(WORKSTATION.items()):
                    SAVED[eid]=cur; after=dict(cur,state="unavailable",attributes={},last_updated=datetime.now(UTC).isoformat())
                    WORKSTATION[eid]=after; stats["workstation_unavailable_events"]+=1
                    await socket.send_json({"id":message.get("id"),"type":"event","event":{"event_type":"state_changed","data":{"entity_id":eid,"old_state":cur,"new_state":after}}})
            if mode!="offline" and SAVED:
                # Availability online: HA restores cached values (NOT new measurements).
                for eid,old in list(SAVED.items()):
                    cur=WORKSTATION[eid]; WORKSTATION[eid]=dict(old,last_updated=datetime.now(UTC).isoformat()); stats["workstation_restore_events"]+=1
                    await socket.send_json({"id":message.get("id"),"type":"event","event":{"event_type":"state_changed","data":{"entity_id":eid,"old_state":cur,"new_state":WORKSTATION[eid]}}})
                SAVED.clear()
                # Like HASS.Agent, the first fresh publish follows the reconnect a moment later.
                stats["workstation_hold_until"]=elapsed+6
            for key,interval in SCHEDULE:
                if mode!="live" or elapsed<stats.get("workstation_hold_until",0): stats["tick_"+key]=elapsed//interval; continue
                tick=elapsed//interval
                eid=MAPPING[key]
                if tick<=stats.get("tick_"+key,0): continue
                stats["tick_"+key]=tick
                before=json.loads(json.dumps(WORKSTATION[eid])); after=json.loads(json.dumps(before))
                if key=="disk_usage":
                    used=504296.0-tick*1200; after["attributes"].update(UsedSpaceMB=used,AvailableSpaceMB=510669.0-used,UsedSpacePercentage=round(used/510669.0*100),AvailableSpacePercentage=100-round(used/510669.0*100))
                elif key=="charging":after["state"]="Charging" if tick%2 else "High"
                elif key=="heartbeat":after["state"]=str(tick%100)
                else:after["state"]=str({"cpu_usage":14,"ram_usage":65,"gpu_usage":8,"battery_percent":90}[key]+tick%8)
                after["last_updated"]=datetime.now(UTC).isoformat();after["last_changed"]=after["last_updated"]
                WORKSTATION[eid]=after
                stats["workstation_"+key+"_changes"]+=1
                SOURCE_EVENTS.append({"t":time.time(),"key":key,"old":before["state"],"new":after["state"],"attrs_changed":before["attributes"]!=after["attributes"]})
                await socket.send_json({"id":message.get("id"),"type":"event","event":{"event_type":"state_changed","data":{"entity_id":eid,"old_state":before,"new_state":after}}})
            # Duplicate heartbeat frames must not refresh liveness or emit device events.
            if elapsed%10==0 and mode=="live":
                h=WORKSTATION[MAPPING["heartbeat"]];stats["duplicate_heartbeat_frames"]+=1
                await socket.send_json({"id":message.get("id"),"type":"event","event":{"event_type":"state_changed","data":{"entity_id":h["entity_id"],"old_state":h,"new_state":h}}})
        await socket.close()
    except Exception:
        pass


@app.post("/_control")
async def control(request: Request):
    scenario.update(await request.json())
    return scenario


@app.get("/_stats")
async def get_stats():
    return {"stats": dict(stats), "scenario": scenario,"source_events":SOURCE_EVENTS}
