"""Decision engine: agronomic logic + safety rules."""
from dataclasses import replace
from datetime import date, datetime, time

import pytest

from app.agronomy import kc
from app.agronomy.reference_data import CROPS
from app.decision import CropInputs, DecisionInputs, decide
from app.weather import WeatherInputs

TOMATO = CROPS["tomato"]


def base_inputs(**over) -> DecisionInputs:
    crop = CropInputs(
        name="Tomato",
        curve=kc.KcCurve(TOMATO.kc_ini, TOMATO.kc_mid, TOMATO.kc_end, TOMATO.l_ini, TOMATO.l_dev,
                         TOMATO.l_mid, TOMATO.l_late),
        max_height_m=TOMATO.max_height_m, zr_ini_m=TOMATO.zr_ini_m, zr_max_m=TOMATO.zr_max_m,
        p=TOMATO.p, source="FAO-56",
    )
    inp = DecisionInputs(
        now_utc=datetime(2026, 7, 15, 5, 30),          # 06:30 Tunis -> inside the 05:00-09:00 window
        timezone="Africa/Tunis", zone_name="Z1", zone_enabled=True, strategy="adaptive",
        planting_date=date(2026, 5, 1), crop=crop, identification_status="confirmed",
        theta_fc=0.25, theta_wp=0.12, soil_source="loam", root_depth_override_m=None,
        area_m2=10.0, efficiency=0.9, efficiency_source="drip", flow_rate_lpm=4.0, refill_fraction=1.0,
        soil_theta=0.17, soil_reading_age_min=5, soil_reading_issues=[], tank_level_pct=80,
        weather_today=WeatherInputs(day=date(2026, 7, 15), et0_mm=6.5, et0_kind="forecast", et0_source="test",
                                    precip_mm=0, precip_probability_pct=0, rain_known=True, rh_min_pct=35,
                                    u2_ms=2.5, tmax_c=34, tmin_c=22),
        rain_tomorrow_mm=0.0, rain_tomorrow_probability_pct=5,
        mode="auto", emergency_stop=False, blocking_anomalies=[], active_command=False,
        irrigation_windows=[(time(5), time(9)), (time(18), time(21))],
        rain_postpone_probability_pct=60, reading_max_age_min=30, min_tank_level_pct=10,
        max_volume_per_event_l=500, timeout_factor=1.5,
    )
    return replace(inp, **over)


def test_irrigate_now_volume_matches_fao_calculation():
    r = decide(base_inputs(max_volume_per_event_l=1000))
    assert r.action == "irrigate_now"
    v = r.values
    # 75 days after planting -> mid-season, full root depth 0.7 m
    assert v["growth_stage"]["value"] == "mid_season"
    assert v["root_depth"]["value"] == pytest.approx(0.7)
    taw = 1000 * (0.25 - 0.12) * 0.7
    dr = 1000 * (0.25 - 0.17) * 0.7
    assert v["taw"]["value"] == pytest.approx(taw, abs=1e-3)
    assert v["depletion"]["value"] == pytest.approx(dr, abs=1e-3)
    assert r.target_volume_l == pytest.approx(dr / 0.9 * 10, abs=0.01)
    # provenance labels
    assert v["soil_moisture"]["kind"] == "measured"
    assert v["et0"]["kind"] == "forecast"
    assert v["required_volume"]["kind"] == "calculated"


def test_no_irrigation_when_depletion_below_raw():
    r = decide(base_inputs(soil_theta=0.24))
    assert r.action == "no_irrigation"
    assert "days_until_irrigation" in r.values


def test_different_crops_give_different_recommendations():
    pepper = CROPS["pepper"]
    pep_crop = CropInputs(name="Pepper", curve=kc.KcCurve(pepper.kc_ini, pepper.kc_mid, pepper.kc_end, pepper.l_ini,
                                                          pepper.l_dev, pepper.l_mid, pepper.l_late),
                          max_height_m=pepper.max_height_m, zr_ini_m=pepper.zr_ini_m, zr_max_m=pepper.zr_max_m,
                          p=pepper.p, source="FAO-56")
    t = decide(base_inputs(soil_theta=0.217))
    p = decide(base_inputs(soil_theta=0.217, crop=pep_crop))
    # Same soil and moisture: pepper (p=0.30, shallower roots) needs water, tomato does not yet
    assert t.action == "no_irrigation"
    assert p.action == "irrigate_now"


@pytest.mark.parametrize("override,needle", [
    ({"emergency_stop": True}, "Emergency stop"),
    ({"mode": "manual"}, "manual mode"),
    ({"identification_status": "needs_confirmation"}, "not confirmed"),
    ({"soil_theta": None}, "No soil moisture"),
    ({"soil_reading_issues": ["ADC limit"]}, "Invalid soil moisture"),
    ({"soil_reading_age_min": 120}, "older than"),
    ({"tank_level_pct": 5}, "tank level"),
    ({"blocking_anomalies": ["leak"]}, "Open anomaly"),
    ({"active_command": True}, "already pending"),
])
def test_safety_rules_block(override, needle):
    r = decide(base_inputs(**override))
    assert r.action == "blocked"
    assert any(needle.lower() in reason.lower() for reason in r.reasons)
    assert r.target_volume_l is None


def test_postpone_when_rain_covers_requirement():
    r = decide(base_inputs(rain_tomorrow_mm=60.0, rain_tomorrow_probability_pct=80))
    assert r.action == "postpone_rain"


def test_no_postpone_when_rain_too_small_or_unlikely():
    assert decide(base_inputs(rain_tomorrow_mm=5.0, rain_tomorrow_probability_pct=90)).action == "irrigate_now"
    assert decide(base_inputs(rain_tomorrow_mm=60.0, rain_tomorrow_probability_pct=30)).action == "irrigate_now"


def test_outside_window_schedules_next_window():
    r = decide(base_inputs(now_utc=datetime(2026, 7, 15, 11, 0)))  # 12:00 Tunis
    assert r.action == "schedule"
    assert r.scheduled_for_utc == datetime(2026, 7, 15, 17, 0)   # 18:00 Tunis


def test_outside_window_irrigates_now_if_waiting_exhausts_water():
    # Very dry: projected depletion at 18:00 exceeds TAW
    r = decide(base_inputs(now_utc=datetime(2026, 7, 15, 11, 0), soil_theta=0.122))
    assert r.action == "irrigate_now"


def test_volume_safety_cap():
    r = decide(base_inputs(max_volume_per_event_l=50))
    assert r.target_volume_l == 50
    assert any("capped" in x for x in r.reasons)


def test_fixed_strategy_zone_never_creates_adaptive_irrigation():
    r = decide(base_inputs(strategy="fixed"))
    assert r.action == "fixed_schedule"
