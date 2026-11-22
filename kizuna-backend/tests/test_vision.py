"""Vision observations: identification, crop-health alerts, rain now and rain forecast."""
from datetime import date, timedelta

from sqlalchemy import inspect, text
from sqlmodel import Session, select

from app import services
from app.db import _add_missing_columns, engine
from app.models import WeatherDay, utcnow

KEY = {"X-Device-Key": "test-key"}


def readings(client, temp, rain=None):
    ctrl = {"air_temp_c": temp, "tank_level_pct": 80, "flow_lpm": 0, "valves_open": []}
    if rain is not None:
        ctrl["rain_sensor_wet"] = rain
    client.post("/api/controller/readings", headers=KEY,
                json={"controller": ctrl, "zones": [{"valve_channel": 0, "soil_raw": 2514},
                                                    {"valve_channel": 1, "soil_raw": 2514}]})


def obs(**over):
    base = {"zone_id": 1, "source": "mast", "canopy": {"canopy_cover_pct": 42.0},
            "crop": {"key": "tomato", "label": "Tomato", "confidence": 0.91},
            "stage": {"key": "development", "confidence": 0.7},
            "pests": [{"label": "healthy", "confidence": 0.8}], "animals": [],
            "rain": {"raining_now": False, "confidence": 0.9}, "engines": {"crop": "test"}}
    base.update(over)
    return base


def test_vision_requires_device_key(client):
    assert client.post("/api/vision/observations", json=obs()).status_code == 401


def test_confident_consistent_crop_is_confirmed_no_alerts(client):
    r = client.post("/api/vision/observations", headers=KEY, json=obs()).json()
    assert r["identification"]["status"] == "confirmed" and r["alerts"] == []
    saved = client.get("/api/vision/observations?zone_id=1").json()[0]
    assert saved["canopy_cover_pct"] == 42.0 and saved["crop_key"] == "tomato"


def test_low_confidence_crop_needs_farmer(client):
    r = client.post("/api/vision/observations", headers=KEY,
                    json=obs(crop={"key": "tomato", "confidence": 0.55})).json()
    assert r["identification"]["status"] == "needs_confirmation"


def test_pest_animal_and_trap_alerts(client):
    r = client.post("/api/vision/observations", headers=KEY, json=obs(
        plant_ref="Z1-P04",
        pests=[{"label": "leaf_miner", "confidence": 0.74, "engine": "clip"}, {"label": "yellowing", "confidence": 0.2}],
        animals=[{"label": "bird", "confidence": 0.81}])).json()
    kinds = {a["kind"] for a in r["alerts"]}
    assert kinds == {"pest:leaf_miner", "animal:bird"}
    assert "Z1-P04" in r["alerts"][0]["message"]
    # sticky trap: 4 then 19 insects -> 15 new -> alert (threshold 10)
    t1 = client.post("/api/vision/observations", headers=KEY,
                     json={"zone_id": 1, "source": "trap", "trap": {"trap_found": True, "insect_count": 4}}).json()
    assert t1["alerts"] == []
    t2 = client.post("/api/vision/observations", headers=KEY,
                     json={"zone_id": 1, "source": "trap", "trap": {"trap_found": True, "insect_count": 19}}).json()
    assert t2["alerts"][0]["kind"] == "insect_trap" and "15 new" in t2["alerts"][0]["message"]
    # crop-health alerts never block irrigation
    readings(client, 22.0)
    readings(client, 33.0)
    assert client.post("/api/zones/1/decide").json()["action"] == "irrigate_now"


def test_rain_now_from_camera_or_sensor_postpones(client):
    readings(client, 22.0)
    readings(client, 33.0)
    client.post("/api/vision/observations", headers=KEY, json=obs(rain={"raining_now": True, "confidence": 0.9}))
    d = client.post("/api/zones/1/decide").json()
    assert d["action"] == "postpone_rain" and "camera" in d["reasons"][0]
    assert d["values"]["raining_now"]["kind"] == "ai_prediction"
    # the rain sensor wins over the camera: dry sensor -> irrigate
    readings(client, 30.0, rain=False)
    d = client.post("/api/zones/1/decide").json()
    assert d["action"] == "irrigate_now"
    client.post(f"/api/commands/{d['command_id']}/stop")
    readings(client, 30.0, rain=True)
    d = client.post("/api/zones/1/decide").json()
    assert d["action"] == "postpone_rain" and d["values"]["raining_now"]["kind"] == "measured"


def test_low_confidence_visual_rain_is_ignored(client):
    readings(client, 22.0)
    readings(client, 33.0)
    client.post("/api/vision/observations", headers=KEY, json=obs(rain={"raining_now": True, "confidence": 0.6}))
    assert client.post("/api/zones/1/decide").json()["action"] == "irrigate_now"


def test_rain_forecast_notification_and_clear(client):
    with Session(engine) as s:
        tomorrow = services.local_today(utcnow(), services.default_settings) + timedelta(days=1)
        s.add(WeatherDay(day=tomorrow, et0_mm=5, precip_mm=8.0, precip_probability_pct=85))
        s.commit()
        a = services.rain_forecast_alert(s)
        assert a is not None and "8.0 mm" in a.message and a.blocks_irrigation is False
        wd = s.exec(select(WeatherDay).where(WeatherDay.day == tomorrow)).one()
        wd.precip_probability_pct = 10
        s.add(wd)
        s.commit()
        assert services.rain_forecast_alert(s) is None
    assert all(x["kind"] != "rain_forecast" for x in client.get("/api/anomalies").json())


def test_migration_adds_new_columns_to_old_database(client):
    with engine.begin() as c:
        c.execute(text('ALTER TABLE sensorreading DROP COLUMN rain_sensor_wet'))
    assert "rain_sensor_wet" not in {c["name"] for c in inspect(engine).get_columns("sensorreading")}
    _add_missing_columns()
    assert "rain_sensor_wet" in {c["name"] for c in inspect(engine).get_columns("sensorreading")}


def test_potato_is_known_and_unknown_goes_to_farmer(client):
    # potato now has an FAO-56 profile
    assert any(c["key"] == "potato" for c in client.get("/api/crops").json())
    # vision abstained (pepper vs potato too close): farmer confirms, zone 1 keeps its tomato crop
    r = client.post("/api/vision/observations", headers=KEY, json=obs(
        crop={"key": "unknown", "best_guess": "pepper", "confidence": 0.46, "label": "Not sure"})).json()
    assert r["identification"]["status"] == "needs_confirmation"
    assert any(a["kind"] == "identification" for a in client.get("/api/anomalies").json())
    zone = client.get("/api/zones").json()[0]
    assert zone["identification_status"] == "confirmed"


def test_untrained_zero_shot_pest_never_raises_an_alert(client):
    r = client.post("/api/vision/observations", headers=KEY, json={
        "zone_id": 1, "source": "phone",
        "pests": [{"label": "leaf_miner", "confidence": 0.99, "engine": "clip-zero-shot ViT-B-32/laion2b"}],
        "health": {"hint": "leaf_miner", "kind": "ai_hint_untrained",
                   "message_fr": "Diagnostic incertain — inspection nécessaire"}}).json()
    assert r["alerts"] == [] and r["health_hint"]["kind"] == "ai_hint_untrained"


def test_an_open_alert_is_not_reported_as_new_on_every_photo(client):
    body = {"zone_id": 1, "source": "carriage",
            "pests": [{"label": "tuta_absoluta_mine", "confidence": 0.8, "engine": "yolo pest model"}]}
    first = client.post("/api/vision/observations", headers=KEY, json=body).json()["alerts"]
    second = client.post("/api/vision/observations", headers=KEY, json=body).json()["alerts"]
    assert first[0]["new"] is True and second[0]["new"] is False and first[0]["id"] == second[0]["id"]
