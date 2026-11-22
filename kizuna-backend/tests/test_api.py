"""End-to-end: ESP32 readings -> decision -> command -> delivery report -> verification."""
from datetime import timedelta

from sqlmodel import Session

from app import services
from app.db import engine
from app.models import IrrigationCommand, utcnow

KEY = {"X-Device-Key": "test-key"}
RAW_THETA_015 = 2514  # with the demo calibration (3000 -> 0.05, 1300 -> 0.40)


def post_readings(client, raw0=RAW_THETA_015, raw1=RAW_THETA_015, temp=30.0, tank=80.0, flow=0.0, valves=None):
    body = {"controller": {"air_temp_c": temp, "air_rh_pct": 40, "tank_level_pct": tank, "flow_lpm": flow,
                           "valves_open": valves or []},
            "zones": [{"valve_channel": 0, "soil_raw": raw0}, {"valve_channel": 1, "soil_raw": raw1}]}
    r = client.post("/api/controller/readings", json=body, headers=KEY)
    assert r.status_code == 200, r.text
    return r.json()


def test_device_key_required(client):
    assert client.get("/api/controller/next-command").status_code == 401
    assert client.get("/api/controller/next-command", headers=KEY).json()["action"] == "idle"


def test_full_irrigation_cycle_and_verification(client):
    post_readings(client, temp=22.0)
    out = post_readings(client, temp=33.0)  # two temperatures -> offline Hargreaves ET0
    assert all(z["valid"] for z in out["stored"])
    assert abs(out["stored"][1]["soil_theta"] - 0.15) < 0.001

    d = client.post("/api/zones/1/decide").json()
    assert d["action"] == "irrigate_now", d["reasons"]
    assert d["values"]["et0"]["kind"] == "calculated"          # offline fallback, labelled as such
    assert any("Rain forecast unavailable" in r for r in d["reasons"])
    target = d["target_volume_l"]
    assert target > 0

    cmd = client.get("/api/controller/next-command", headers=KEY).json()
    assert cmd["action"] == "irrigate" and cmd["valve_channel"] == 0
    assert abs(cmd["target_volume_l"] - target) < 1e-6
    # polling again while irrigating returns the same command (idempotent)
    assert client.get("/api/controller/next-command", headers=KEY).json()["command_id"] == cmd["command_id"]
    client.post(f"/api/controller/commands/{cmd['command_id']}/started", headers=KEY)
    rep = client.post(f"/api/controller/commands/{cmd['command_id']}/report", headers=KEY,
                      json={"delivered_l": target * 1.03, "stop_reason": "volume_reached"}).json()
    assert rep["status"] == "done" and rep["verification"]["volume_ok"] is True
    assert client.get("/api/anomalies").json() == []

    # Post-irrigation moisture check: soil wetter 40 min later
    with Session(engine) as s:
        later = utcnow() + timedelta(minutes=40)
        services.ingest_readings(s, {"zones": [{"valve_channel": 0, "soil_raw": 2100}]}, now=later)
        assert services.check_moisture_responses(s, now=later) == 1
        c = s.get(IrrigationCommand, cmd["command_id"])
        assert c.theta_after > c.theta_before
        assert c.verification["response_ratio"] is not None


def test_low_delivery_creates_blocking_anomaly(client):
    post_readings(client, temp=22.0)
    post_readings(client, temp=33.0)
    d = client.post("/api/zones/2/decide").json()
    assert d["action"] == "irrigate_now", d["reasons"]
    cmd = client.get("/api/controller/next-command", headers=KEY).json()
    client.post(f"/api/controller/commands/{cmd['command_id']}/report", headers=KEY,
                json={"delivered_l": d["target_volume_l"] * 0.4, "stop_reason": "timeout"})
    anomalies = client.get("/api/anomalies").json()
    assert anomalies[0]["kind"] == "low_delivery" and anomalies[0]["blocks_irrigation"]
    d2 = client.post("/api/zones/2/decide").json()
    assert d2["action"] == "blocked"
    client.post(f"/api/anomalies/{anomalies[0]['id']}/ack")
    assert client.post("/api/zones/2/decide").json()["action"] == "irrigate_now"


def test_leak_detection(client):
    post_readings(client, flow=2.0, valves=[])
    a = client.get("/api/anomalies").json()
    assert a[0]["kind"] == "leak" and a[0]["severity"] == "critical"
    post_readings(client, temp=33.0)
    assert client.post("/api/zones/1/decide").json()["action"] == "blocked"


def test_invalid_sensor_blocks_irrigation(client):
    post_readings(client, raw0=4095, temp=22.0)
    post_readings(client, raw0=4095, temp=33.0)
    d = client.post("/api/zones/1/decide").json()
    assert d["action"] == "blocked"
    assert any("Invalid soil moisture" in r for r in d["reasons"])
    assert any(a["kind"] == "sensor" for a in client.get("/api/anomalies").json())


def test_emergency_stop_and_manual_mode(client):
    post_readings(client, temp=22.0)
    post_readings(client, temp=33.0)
    client.post("/api/zones/1/decide")
    assert client.post("/api/system/emergency-stop", json={"active": True}).json()["emergency_stop"]
    assert client.get("/api/controller/next-command", headers=KEY).json()["action"] == "stop_all"
    assert client.post("/api/zones/1/manual-irrigation", json={"volume_l": 5}).status_code == 409
    client.post("/api/system/emergency-stop", json={"active": False})
    assert client.get("/api/controller/next-command", headers=KEY).json()["action"] == "idle"  # cancelled

    client.post("/api/system/mode", json={"mode": "manual"})
    assert client.post("/api/zones/1/decide").json()["action"] == "blocked"
    r = client.post("/api/zones/1/manual-irrigation", json={"volume_l": 5})
    assert r.status_code == 201 and r.json()["origin"] == "manual"
    assert client.get("/api/controller/next-command", headers=KEY).json()["action"] == "irrigate"


def test_identification_confirmation_flow(client):
    post_readings(client, temp=22.0)
    post_readings(client, temp=33.0)
    # Zone 1 is a confirmed tomato zone: a doubtful AI photo goes to the farmer but does NOT stop irrigation
    low = client.post("/api/zones/1/identifications",
                      json={"species_key": "tomato", "confidence": 0.55, "model_name": "demo"}).json()
    assert low["status"] == "needs_confirmation"
    d = client.post("/api/zones/1/decide").json()
    assert d["action"] == "irrigate_now"
    client.post(f"/api/commands/{d['command_id']}/stop")
    # Confident AI result that disagrees with the registered crop still needs the farmer
    other = client.post("/api/zones/1/identifications", json={"species_key": "pepper", "confidence": 0.97}).json()
    assert other["status"] == "needs_confirmation"
    msgs = [a["message"] for a in client.get("/api/anomalies").json() if a["kind"] == "identification"]
    assert msgs and "Irrigation continues for Tomato" in msgs[0]
    assert client.post("/api/zones/1/decide").json()["action"] == "irrigate_now"

    # A zone without a confirmed crop is blocked until the farmer confirms
    z = client.get("/api/zones").json()[1]
    z.update({"crop_key": None}); z.pop("id")
    with Session(engine) as s:
        from app.models import Zone
        zone = s.get(Zone, 2)
        zone.crop_profile_id, zone.identification_status = None, "unconfirmed"
        s.add(zone)
        s.commit()
    unsure = client.post("/api/zones/2/identifications", json={"species_key": "pepper", "confidence": 0.6}).json()
    assert unsure["status"] == "needs_confirmation"
    assert client.post("/api/zones/2/decide").json()["action"] == "blocked"
    client.post(f"/api/identifications/{unsure['id']}/confirm", json={})
    assert client.post("/api/zones/2/decide").json()["action"] == "irrigate_now"
    # Confident and consistent -> accepted automatically
    again = client.post("/api/zones/2/identifications", json={"species_key": "pepper", "confidence": 0.95}).json()
    assert again["status"] == "confirmed"


def test_fixed_schedule_zone_and_comparison(client):
    z = client.get("/api/zones").json()[1]
    z.update({"strategy": "fixed", "fixed_volume_l": 20, "fixed_time": "00:00", "crop_key": "pepper"})
    z.pop("id"); z.pop("crop_profile_id"); z.pop("identification_status")
    assert client.put("/api/zones/2", json=z).status_code == 200
    post_readings(client, temp=22.0)
    post_readings(client, temp=33.0)
    with Session(engine) as s:
        created = services.run_fixed_schedules(s)
        assert len(created) == 1 and created[0].origin == "fixed"
        assert services.run_fixed_schedules(s) == []  # once per day
    client.post("/api/zones/1/decide")
    for _ in range(2):
        cmd = client.get("/api/controller/next-command", headers=KEY).json()
        client.post(f"/api/controller/commands/{cmd['command_id']}/report", headers=KEY,
                    json={"delivered_l": cmd["target_volume_l"], "stop_reason": "volume_reached"})
    rep = client.get("/api/report/comparison?days=1").json()
    assert rep["comparison"]["available"] is True
    assert rep["comparison"]["kind"] == "measured"
    assert rep["comparison"]["same_crop"] is False
    assert all(z["water_delivered_l"]["kind"] == "measured" for z in rep["zones"])


def test_decide_all_returns_full_objects(client):
    post_readings(client, temp=22.0)
    post_readings(client, temp=33.0)
    out = client.post("/api/decide-all").json()
    assert len(out) == 2 and all("action" in d and "zone_id" in d for d in out)
