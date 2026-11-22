"""Dashboard: the two pages are served and their summary endpoints reflect the real state."""
from datetime import timedelta

from sqlmodel import Session, select

from app.models import WeatherDay

KEY = {"X-Device-Key": "test-key"}


def _engine():
    from app.db import engine
    return engine


def _forecast():
    from app.growth import farm_today
    with Session(_engine()) as s:
        for i in range(7):
            s.add(WeatherDay(day=farm_today() + timedelta(days=i), et0_mm=5.0, precip_mm=0, precip_probability_pct=5,
                             tmax_c=30, tmin_c=18, rh_min_pct=45, u2_ms=2))
        s.commit()


def _readings(client, raw=2300, flow=0.0, valves=None):
    client.post("/api/controller/readings", headers=KEY, json={
        "controller": {"air_temp_c": 26.0, "air_rh_pct": 50, "tank_level_pct": 80, "flow_lpm": flow,
                       "valves_open": valves or [], "rain_sensor_wet": False},
        "zones": [{"valve_channel": 0, "soil_raw": raw}, {"valve_channel": 1, "soil_raw": raw}]})


def test_root_points_to_the_separate_dashboard(client):
    r = client.get("/").json()
    assert "8080" in r["dashboard"] and r["api_docs"] == "/docs"
    assert client.get("/dashboard/").status_code == 404          # the pages are no longer served here


def test_farm_map_layout_defaults_save_and_reset(client):
    lay = client.get("/api/dashboard/layout").json()
    assert lay["saved"] is False and {"tank", "esp32", "zone:1", "zone:2"} <= set(lay["items"])
    moved = dict(lay["items"], **{"zone:2": {"x": 900, "y": 500}})
    assert client.put("/api/dashboard/layout", json={"data": {"items": moved}}).json()["saved"]
    lay = client.get("/api/dashboard/layout").json()
    assert lay["saved"] and lay["items"]["zone:2"] == {"x": 900.0, "y": 500.0}
    # a zone created later gets a default place, unknown keys are ignored
    client.post("/api/zones", json={"name": "Zone 3 - Potato", "valve_channel": 2, "crop_key": "potato"})
    client.put("/api/dashboard/layout", json={"data": {"items": {**moved, "junk": {"x": 1, "y": 2}}}})
    lay = client.get("/api/dashboard/layout").json()
    assert "zone:3" in lay["items"] and "junk" not in lay["items"]
    client.delete("/api/dashboard/layout")
    assert client.get("/api/dashboard/layout").json()["saved"] is False


def test_overview_without_esp32_says_offline_and_lists_missing_sensors(client):
    d = client.get("/api/dashboard/overview").json()
    assert d["system"]["controller_online"] is False
    z = d["zones"][1]
    assert [s["key"] for s in z["flow"]] == ["sensors", "fao", "decision", "valve", "flow", "check"]
    assert z["flow"][0]["state"] == "error"
    sensors = {s["key"]: s for s in z["sensors"]}
    assert len(sensors) == 9
    assert sensors["light"]["status"] == sensors["ph"]["status"] == "not_installed"
    assert sensors["soil_moisture"]["status"] == "no_data"


def test_overview_follows_the_esp32_and_the_valve(client):
    _forecast()
    _readings(client)
    client.post("/api/zones/2/decide")
    d = client.get("/api/dashboard/overview").json()
    z = d["zones"][1]
    assert d["system"]["controller_online"] and z["flow"][0]["state"] == "ok"
    assert z["flow"][1]["kind"] == "calculated" and "ETc" in z["flow"][1]["value"]
    assert "Réserve utilisée" in z["decision"]["summary_fr"] or z["decision"]["action"] != "no_irrigation"
    assert z["plan"]["frequency"]["text_fr"] and len(z["plan_daily"]) == 7
    assert {s["key"]: s for s in z["sensors"]}["soil_moisture"]["kind"] == "measured"
    # open a valve: manual command, ESP32 starts it and reports flow
    cmd = client.post("/api/zones/2/manual-irrigation", json={"volume_l": 30}).json()
    client.get("/api/controller/next-command", headers=KEY)
    client.post(f"/api/controller/commands/{cmd['id']}/started", headers=KEY)
    _readings(client, flow=4.0, valves=[1])
    z = client.get("/api/dashboard/overview").json()["zones"][1]
    states = {s["key"]: s["state"] for s in z["flow"]}
    assert states["valve"] == "active" and states["flow"] == "active"
    assert z["plan"]["status"] == "irrigating" and z["plan"]["next_irrigation"] is None


def test_emergency_stop_shows_on_the_valve_station(client):
    _readings(client)
    client.post("/api/system/emergency-stop", json={"active": True})
    z = client.get("/api/dashboard/overview").json()["zones"][0]
    v = next(s for s in z["flow"] if s["key"] == "valve")
    assert v["state"] == "error" and "urgence" in v["value"]


def test_history_for_the_chart(client):
    _forecast()
    _readings(client)
    client.post("/api/zones/2/decide")
    h = client.get("/api/dashboard/zones/2/history?hours=24").json()
    assert h["soil"] and h["needs"] and h["theta_fc"] > h["theta_wp"]
    assert "root_depth_m" in h["needs"][0]


def test_camera_procedure_steps_and_disagreement(client):
    _forecast()
    _readings(client)
    client.post("/api/vision/observations", headers=KEY, json={
        "zone_id": 2, "source": "phone", "image_ref": "x.jpg", "crop": {"key": "pepper", "confidence": 0.85},
        "stage": {"key": "flowers", "confidence": 0.8, "visual_cues": ["open white flowers"]},
        "health": {"hint": "healthy", "kind": "ai_hint_untrained", "message_fr": "Aucun problème visible suggéré"}})
    d = client.get("/api/dashboard/camera").json()
    o = d["observations"][0]
    steps = {s["key"]: s for s in o["steps"]}
    assert list(steps) == ["photo", "crop", "visual_stage", "health", "sensors", "stage", "plan", "farmer"]
    assert steps["crop"]["value"] == "Poivron" and steps["visual_stage"]["value"] == "en fleurs"
    assert steps["sensors"]["state"] == "ok" and "sol" in steps["sensors"]["detail"]
    # the young pepper zone: calendar says establishment, the photo says flowering -> ask the farmer
    assert steps["stage"]["status"] == "needs_confirmation" and steps["stage"]["calendar_stage"]["key"] == "establishment"
    assert steps["farmer"]["state"] == "warn"
    assert o["current_plan"]["frequency"]
    # keeping the calendar stage from the camera page settles it
    client.post("/api/zones/2/stage/confirm", json={"stage_key": "establishment"})
    o = client.get(f"/api/dashboard/camera/{o['id']}").json()
    assert {s["key"]: s for s in o["steps"]}["farmer"]["state"] == "ok"


def test_camera_photo_of_another_crop_gets_no_plan(client):
    client.post("/api/vision/observations", headers=KEY, json={
        "zone_id": 1, "source": "phone", "crop": {"key": "pepper", "confidence": 0.9}, "stage": {"key": "flowers", "confidence": 0.8}})
    o = client.get("/api/dashboard/camera").json()["observations"][0]
    steps = {s["key"]: s for s in o["steps"]}
    assert steps["crop"]["state"] == "warn" and "confirmer" in steps["crop"]["detail"]
    assert steps["plan"]["state"] == "idle"


def test_trap_photo_has_its_own_short_procedure(client):
    client.post("/api/vision/observations", headers=KEY, json={"zone_id": 1, "source": "trap",
                                                               "trap": {"trap_found": True, "insect_count": 12}})
    o = client.get("/api/dashboard/camera").json()["observations"][0]
    assert [s["key"] for s in o["steps"]] == ["photo", "trap"] and "12" in o["steps"][1]["value"]


def test_camera_endpoint_lists_the_field_cameras(client):
    assert client.get("/api/dashboard/camera").json()["cameras"] == []
    for _ in range(3):
        client.post("/api/vision/observations", headers=KEY, json={"zone_id": 2, "source": "mast", "image_ref": "a.jpg",
                                                                   "crop": {"key": "pepper", "confidence": 0.9}})
    client.post("/api/vision/observations", headers=KEY, json={"zone_id": 1, "source": "phone"})
    cams = client.get("/api/dashboard/camera").json()["cameras"]
    assert len(cams) == 1 and cams[0]["source"] == "mast" and cams[0]["zone_id"] == 2
    assert cams[0]["last_image_ref"] == "a.jpg" and cams[0]["state"] in ("ok", "single")


def test_overview_gives_the_cycle_progress_and_the_camera_summary(client):
    _forecast()
    _readings(client)
    client.post("/api/zones/2/decide")
    client.post("/api/vision/observations", headers=KEY, json={
        "zone_id": 2, "source": "mast", "image_ref": "cam.jpg", "crop": {"key": "pepper", "confidence": 0.8},
        "stage": {"key": "seedling", "confidence": 0.7},
        "health": {"hint": "healthy", "kind": "ai_hint_untrained", "message_fr": "Aucun problème visible suggéré"}})
    z = client.get("/api/dashboard/overview").json()["zones"][1]
    assert z["vision"]["crop_fr"] == "Poivron" and z["vision"]["stage_fr"] == "jeune plant" and z["vision"]["health_ok"]
    p = z["procedure"]
    assert [s["key"] for s in p["steps"]] == ["measure", "observe", "calculate", "decide", "irrigate", "verify"]
    assert p["current"] == "measure" and p["steps"][0]["state"] == "ok" and p["steps"][1]["state"] == "ok"
    # an irrigation running moves the cycle to "irrigate"
    cmd = client.post("/api/zones/2/manual-irrigation", json={"volume_l": 30}).json()
    client.get("/api/controller/next-command", headers=KEY)
    client.post(f"/api/controller/commands/{cmd['id']}/started", headers=KEY)
    p = client.get("/api/dashboard/overview").json()["zones"][1]["procedure"]
    assert p["current"] == "irrigate" and p["steps"][4]["state"] == "active"
    # finished: the cycle waits for the soil-moisture check
    client.post(f"/api/controller/commands/{cmd['id']}/report", headers=KEY,
                json={"delivered_l": 29.5, "stop_reason": "volume_reached"})
    p = client.get("/api/dashboard/overview").json()["zones"][1]["procedure"]
    assert p["current"] == "verify" and "volume OK" in p["steps"][5]["value"]
