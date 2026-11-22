"""Step 1: crop-specific growth cycles, stage evidence fusion and history."""
from datetime import date, timedelta

import pytest

from app.agronomy.crop_cycles import CYCLES, cycle_for
from app.agronomy.reference_data import CROPS
from app.growth import calendar_position, farm_today

KEY = {"X-Device-Key": "test-key"}


# ------------------------------------------------------------------ cycle definitions
@pytest.mark.parametrize("crop", ["pepper", "potato", "tomato", "eggplant"])
def test_each_cycle_matches_its_fao56_periods(crop):
    c, ref = CYCLES[crop], CROPS[crop]
    assert c.period_days() == {"initial": ref.l_ini, "development": ref.l_dev,
                               "mid_season": ref.l_mid, "late_season": ref.l_late}
    assert not c.generic and len(c.stages) == 5 and c.sources and c.assumptions


def test_pepper_and_potato_never_share_a_cycle():
    pepper = [s.key for s in CYCLES["pepper"].stages]
    potato = [s.key for s in CYCLES["potato"].stages]
    assert pepper == ["establishment", "vegetative", "flowering", "fruit_set", "ripening"]
    assert potato == ["sprouting_emergence", "vegetative", "tuber_initiation", "tuber_bulking", "maturation"]
    assert CYCLES["pepper"] is not CYCLES["potato"]


def test_potato_tuber_stages_are_not_visible_and_have_a_field_check():
    for key in ("tuber_initiation", "tuber_bulking"):
        s = CYCLES["potato"].get(key)
        assert s.visible is False and "Dig" in s.field_check
    assert "twice the stolon diameter" in CYCLES["potato"].get("tuber_initiation").field_check
    # the camera can only say "flowering": compatible with two underground stages
    assert CYCLES["potato"].visual_classes["flowering"] == ("tuber_initiation", "tuber_bulking")


def test_unknown_crop_gets_flagged_fao_fallback():
    c = cycle_for("okra", 20, 30, 40, 20)
    assert c.generic and [s.key for s in c.stages] == ["initial", "development", "mid_season", "late_season"]


@pytest.mark.parametrize("crop,day,expected", [
    ("pepper", 0, "establishment"), ("pepper", 30, "vegetative"), ("pepper", 65, "flowering"),
    ("pepper", 80, "fruit_set"), ("pepper", 105, "ripening"),
    ("potato", 10, "sprouting_emergence"), ("potato", 40, "vegetative"), ("potato", 60, "tuber_initiation"),
    ("potato", 80, "tuber_bulking"), ("potato", 120, "maturation"),
])
def test_calendar_position(crop, day, expected):
    assert calendar_position(CYCLES[crop], day)["stage"].key == expected


# ------------------------------------------------------------------ API helpers
def readings(client):
    for t in (22.0, 33.0):
        client.post("/api/controller/readings", headers=KEY, json={
            "controller": {"air_temp_c": t, "tank_level_pct": 80},
            "zones": [{"valve_channel": 0, "soil_raw": 2514}, {"valve_channel": 1, "soil_raw": 2514}]})


def vision(client, zone_id, crop, visual_class, conf=0.8, plant_ref=None):
    return client.post("/api/vision/observations", headers=KEY, json={
        "zone_id": zone_id, "plant_ref": plant_ref, "source": "phone",
        "crop": {"key": crop, "label": crop, "confidence": 0.9},
        "stage": {"key": visual_class, "confidence": conf, "visual_cues": [f"cue for {visual_class}"]}}).json()


def potato_zone(client, days_ago):
    z = client.post("/api/zones", json={
        "name": "Zone 3 - Potato", "valve_channel": 2, "crop_key": "potato", "soil_type": "sandy_loam",
        "planting_date": (farm_today() - timedelta(days=days_ago)).isoformat(), "area_m2": 10,
        "sensor_raw_dry": 3000, "sensor_theta_dry": 0.05, "sensor_raw_wet": 1300, "sensor_theta_wet": 0.40})
    return z.json()["id"]


# ------------------------------------------------------------------ calendar + photo + farmer
def test_pepper_zone_calendar_stage_and_photo_agreement(client):
    s = client.get("/api/zones/2/stage").json()          # demo pepper planted 20 days ago
    assert s["crop_key"] == "pepper" and s["stage"]["key"] == "establishment"
    assert s["agreement"] == "calendar_only" and s["kind"] == "estimated"
    r = vision(client, 2, "pepper", "seedling")
    assert r["stage"]["agreement"] == "ai_agrees" and not r["stage"]["needs_confirmation"]


def test_photo_disagreeing_with_calendar_asks_the_farmer(client):
    r = vision(client, 2, "pepper", "flowers")
    st = r["stage"]
    assert st["agreement"] == "disagreement" and st["needs_confirmation"]
    assert "Flowering" in st["reasons"][0] and st["stage"]["key"] == "establishment"   # AI did not override


def test_low_confidence_photo_is_not_used(client):
    st = vision(client, 2, "pepper", "flowers", conf=0.4)["stage"]
    assert st["agreement"] == "calendar_only" and "not clear enough" in st["reasons"][0]


def test_farmer_confirmation_shifts_cycle_and_changes_irrigation(client):
    readings(client)
    before = client.post("/api/zones/2/decide").json()
    client.post(f"/api/commands/{before['command_id']}/stop")
    st = client.post("/api/zones/2/stage/confirm", json={"stage_key": "flowering", "note": "first flowers"}).json()
    assert st["stage"]["key"] == "flowering" and st["kind"] == "confirmed" and st["effective_day"] == 60
    after = client.post("/api/zones/2/decide").json()
    v0, v1 = before["values"], after["values"]
    assert v0["growth_stage"]["value"] == "establishment" and v1["growth_stage"]["value"] == "flowering"
    assert v1["growth_stage"]["kind"] == "confirmed" and v1["fao_period"]["value"] == "mid_season"
    assert v1["kc"]["value"] > v0["kc"]["value"]                    # mid-season Kc > initial Kc
    assert v1["root_depth"]["value"] > v0["root_depth"]["value"]    # deeper roots -> more water held
    assert v1["effective_day"]["value"] == 60


def test_stage_of_another_crop_is_refused(client):
    r = client.post("/api/zones/2/stage/confirm", json={"stage_key": "tuber_bulking"})
    assert r.status_code == 422 and "pepper" in r.text


def test_photo_never_moves_the_crop_backwards(client):
    client.post("/api/zones/2/stage/confirm", json={"stage_key": "fruit_set"})
    st = vision(client, 2, "pepper", "seedling")["stage"]
    assert st["stage"]["key"] == "fruit_set" and "earlier stage" in st["reasons"][0]


def test_photo_of_another_crop_is_not_mixed_into_the_cycle(client):
    r = vision(client, 2, "potato", "flowering")
    assert r["stage_evidence_id"] is None


# ------------------------------------------------------------------ potato: underground stages
def test_potato_underground_stage_needs_digging_then_confirmation(client):
    zid = potato_zone(client, 60)
    st = client.get(f"/api/zones/{zid}/stage").json()
    assert st["stage"]["key"] == "tuber_initiation" and st["stage"]["visible"] is False
    assert st["field_check_due"] and st["needs_confirmation"] and "Dig one plant" in " ".join(st["reasons"])
    # photo of flowering potato: compatible, but cannot settle an underground stage
    r = vision(client, zid, "potato", "flowering")
    assert r["stage"]["agreement"] == "ai_compatible" and r["stage"]["field_check_due"]
    ev = r["stage_evidence_id"]
    assert client.post(f"/api/stage-observations/{ev}/confirm", json={}).status_code == 422   # 2 candidates
    ok = client.post(f"/api/stage-observations/{ev}/confirm", json={"stage_key": "tuber_initiation",
                                                                    "note": "dug plant 3: stolon tips swollen"}).json()
    assert ok["kind"] == "confirmed" and not ok["field_check_due"] and not ok["needs_confirmation"]


def test_potato_senescence_photo_agrees_with_maturation(client):
    zid = potato_zone(client, 112)
    assert vision(client, zid, "potato", "senescence")["stage"]["agreement"] == "ai_agrees"


# ------------------------------------------------------------------ history and individual plants
def test_history_records_photos_confirmations_and_transitions(client):
    readings(client)
    d = client.post("/api/zones/2/decide").json()            # records the calendar stage as a transition
    client.post(f"/api/commands/{d['command_id']}/stop")
    vision(client, 2, "pepper", "seedling")
    client.post("/api/zones/2/stage/confirm", json={"stage_key": "vegetative"})
    h = client.get("/api/zones/2/stage/history").json()
    assert [x["source"] for x in h][:3] == ["farmer", "ai", "system"]
    st = client.get("/api/zones/2/stage").json()
    assert st["previous_stage"]["stage"] == "establishment"


def test_individual_plant_has_its_own_stage_and_history(client):
    p = client.post("/api/zones/2/plants", json={
        "label": "Z2-P01", "planting_date": (farm_today() - timedelta(days=70)).isoformat()}).json()
    assert p["has_own_valve"] is False                       # SMART default: watered by the zone valve
    ps = client.get(f"/api/plants/{p['id']}/stage").json()
    zs = client.get("/api/zones/2/stage").json()
    assert ps["stage"]["key"] == "flowering" and zs["stage"]["key"] == "establishment"
    client.post("/api/zones/2/stage/confirm", json={"stage_key": "fruit_set", "plant_id": p["id"]})
    assert client.get(f"/api/plants/{p['id']}/stage").json()["stage"]["key"] == "fruit_set"
    assert client.get("/api/zones/2/stage").json()["stage"]["key"] == "establishment"    # zone untouched
    # camera photo with a plant label: plant found (or created) and evidence attached to it
    vision(client, 2, "pepper", "green_fruits", plant_ref="Z2-P01")
    vision(client, 2, "pepper", "seedling", plant_ref="Z2-P02")
    plants = {x["label"]: x["id"] for x in client.get("/api/zones/2/plants").json()}
    assert set(plants) == {"Z2-P01", "Z2-P02"}
    h1 = client.get(f"/api/plants/{plants['Z2-P01']}/stage/history").json()
    assert h1[0]["visual_class"] == "green_fruits" and h1[0]["plant_id"] == plants["Z2-P01"]


def test_cycle_endpoint_documents_sources_and_assumptions(client):
    c = client.get("/api/crops/potato/cycle").json()
    assert c["generic"] is False and "BBCH_POTATO" in c["sources"]
    assert any("underground" in a for a in c["assumptions"])
    assert set(client.get("/api/cycles").json()) == {"pepper", "potato", "tomato", "eggplant"}
