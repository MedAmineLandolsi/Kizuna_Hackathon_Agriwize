"""Message 8, step 4: irrigation frequency, next irrigation, volume, duration, weekly water."""
from datetime import date, timedelta

import pytest
from sqlmodel import Session, select

from app.models import WeatherDay, Zone

KEY = {"X-Device-Key": "test-key"}


def _engine():
    from app.db import engine
    return engine


def forecast(et0=5.0, rain=0.0, prob=5.0, days=7, rain_day=None):
    from app.growth import farm_today
    today = farm_today()
    with Session(_engine()) as s:
        for i in range(days):
            d = today + timedelta(days=i)
            r = rain if (rain_day is None or i == rain_day) else 0.0
            old = s.exec(select(WeatherDay).where(WeatherDay.day == d)).first()
            if old:
                s.delete(old)
                s.commit()
            s.add(WeatherDay(day=d, et0_mm=et0, precip_mm=r, precip_probability_pct=prob, tmax_c=30, tmin_c=18,
                             rh_min_pct=45, u2_ms=2.0))
        s.commit()


def soil(client, raw):
    client.post("/api/controller/readings", headers=KEY, json={
        "controller": {"air_temp_c": 27.0, "tank_level_pct": 80},
        "zones": [{"valve_channel": 0, "soil_raw": raw}, {"valve_channel": 1, "soil_raw": raw}]})


def plan(client, zone=2):
    r = client.get(f"/api/zones/{zone}/irrigation-plan")
    assert r.status_code == 200
    return r.json()


def test_plan_gives_frequency_next_irrigation_volume_duration_and_week(client):
    forecast(et0=5.0)
    soil(client, 2300)
    p = plan(client)
    assert p["data_kind"] == "real" and not p["missing"]
    assert p["frequency"]["interval_days"] and p["frequency"]["text_en"].startswith("every")
    assert p["irrigations_per_week"] >= 1 and p["events"]
    e = p["events"][0]
    inp = p["inputs"]
    # volume = net / efficiency x area ; duration = volume / flow
    assert e["volume_l"] == pytest.approx(e["net_mm"] / inp["efficiency"] * inp["area_m2"], rel=0.01)
    assert e["duration_min"] == pytest.approx(e["volume_l"] / inp["flow_rate_lpm"], rel=0.01)
    assert p["weekly_water_l"] == pytest.approx(sum(x["volume_l"] for x in p["events"]), rel=0.01)
    assert p["next_irrigation"]["when_fr"] and p["next_stage"]["name_fr"]
    assert len(p["daily"]) == 7 and all(r["et0_kind"] == "forecast" for r in p["daily"])


def test_hotter_weather_means_more_frequent_irrigation(client):
    soil(client, 2000)
    forecast(et0=3.0)
    cool = plan(client)
    forecast(et0=8.0)
    hot = plan(client)
    assert hot["irrigations_per_week"] > cool["irrigations_per_week"]
    assert hot["weekly_crop_demand_l"] > cool["weekly_crop_demand_l"]


def test_frequency_follows_the_stage_and_pepper_flowering_limit(client):
    forecast(et0=5.0)
    soil(client, 2000)
    young = plan(client)
    assert client.post("/api/zones/2/stage/confirm", json={"stage_key": "flowering"}).status_code == 201
    flowering = plan(client)
    day = flowering["daily"][0]
    assert day["stage"] == "flowering" and day["p"] == 0.25            # FAO: ≤ 25 % at start of flowering
    assert day["raw_mm"] == pytest.approx(0.25 * day["taw_mm"], abs=0.1)
    assert day["kc"] > young["daily"][0]["kc"] and day["root_depth_m"] > young["daily"][0]["root_depth_m"]
    assert flowering["weekly_crop_demand_l"] > young["weekly_crop_demand_l"]
    # the decision engine uses the same limit
    d = client.post("/api/zones/2/decide").json()
    assert d["values"]["p"]["value"] == 0.25 and "FAO_CROP_PEPPER" in d["values"]["p"]["source"]


def test_probable_rain_is_counted_unlikely_rain_is_not(client):
    soil(client, 2000)
    forecast(et0=5.0, rain=15.0, prob=90.0, rain_day=2)
    wet = plan(client)
    forecast(et0=5.0, rain=15.0, prob=20.0, rain_day=2)
    unsure = plan(client)
    assert wet["daily"][2]["effective_rain_mm"] == 15.0 and unsure["daily"][2]["effective_rain_mm"] == 0.0
    assert wet["weekly_water_l"] < unsure["weekly_water_l"]


def test_without_soil_reading_frequency_is_given_but_not_the_next_date(client):
    forecast(et0=5.0)
    p = plan(client)
    assert any("soil moisture" in m for m in p["missing"])
    assert p["next_irrigation"] is None and p["frequency"]["interval_days"]


def test_without_any_weather_data_no_recommendation_is_invented(client):
    p = plan(client)
    assert p["status"] == "insufficient_data" and any("ET0" in m for m in p["missing"])
    assert "frequency" not in p


def test_plan_agrees_with_the_decision_engine(client):
    """Same rule: with the all-day window of the tests, a dry soil is irrigated now in both."""
    forecast(et0=5.0)
    soil(client, 2700)                                   # dry
    p = plan(client)
    d = client.post("/api/zones/2/decide").json()
    assert d["action"] == "irrigate_now"
    assert p["events"][0]["at"][:13] == p["generated_at"][:13]          # first event: now
    assert p["events"][0]["volume_l"] == pytest.approx(d["target_volume_l"], rel=0.02)
    client.post(f"/api/commands/{d['command_id']}/stop")
    soil(client, 1450)                                   # wet
    p = plan(client)
    assert client.post("/api/zones/2/decide").json()["action"] == "no_irrigation"
    assert not p["events"] or p["events"][0]["at"][:13] != p["generated_at"][:13]


def test_lower_efficiency_needs_more_water(client):
    forecast(et0=5.0)
    soil(client, 2300)
    base = plan(client)
    with Session(_engine()) as s:
        z = s.get(Zone, 2)
        z.efficiency = base["inputs"]["efficiency"] / 2
        s.add(z); s.commit()
    half = plan(client)
    assert half["volume_per_irrigation_l"] == pytest.approx(2 * base["volume_per_irrigation_l"], rel=0.05)


def test_blocked_system_shows_needs_but_no_next_irrigation(client):
    forecast(et0=5.0)
    soil(client, 2300)
    client.post("/api/system/emergency-stop", json={"active": True})
    p = plan(client)
    assert p["status"] == "blocked" and p["next_irrigation"] is None
    assert any("blocked" in a for a in p["assumptions"])


def test_photo_analysis_returns_stage_next_stage_and_irrigation(client):
    forecast(et0=5.0)
    soil(client, 2300)
    r = client.post("/api/vision/observations", headers=KEY, json={
        "zone_id": 2, "source": "phone", "crop": {"key": "pepper", "confidence": 0.9},
        "stage": {"key": "seedling", "confidence": 0.8}})
    body = r.json()
    irr = body["irrigation"]
    assert irr["for_crop"] == "pepper" and irr["frequency"]["text_fr"]
    assert irr["next_stage"]["name_en"] == "Vegetative growth" and irr["next_stage"]["date"]
    assert irr["volume_per_irrigation_l"] > 0 and irr["duration_per_irrigation_min"] > 0
    assert body["stage"]["stage"]["key"] == "establishment"


def test_photo_of_another_crop_gets_no_irrigation_plan(client):
    forecast(et0=5.0)
    r = client.post("/api/vision/observations", headers=KEY, json={
        "zone_id": 2, "source": "phone", "crop": {"key": "potato", "confidence": 0.9},
        "stage": {"key": "flowering", "confidence": 0.8}})
    irr = r.json()["irrigation"]
    assert irr["status"] == "insufficient_data" and "confirm the crop" in irr["missing"][0]
