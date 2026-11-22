"""Message 8, steps 1-3: degree-days, stage dates, stage needs and certainty."""
import math
import re
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from sqlmodel import Session, select

from app.agronomy.crop_cycles import CYCLES
from app.agronomy.thermal import ThermalParams, equivalent_normal_days, gdd_single_sine
from app.climate import (DayTemp, TemperatureData, parse_archive, refresh_climate, store_climate_days,
                         thermal_progress)
from app.models import ClimateDay, SensorReading, WeatherDay, Zone

PEPPER_T = CYCLES["pepper"].thermal
POTATO_T = CYCLES["potato"].thermal


# ------------------------------------------------------------------ degree-day formula
def _numeric_gdd(tmin, tmax, lo, hi, n=20000):
    """Integrate the sine day numerically: reference for the closed-form single sine method."""
    mean, amp = (tmax + tmin) / 2, (tmax - tmin) / 2
    total = 0.0
    for i in range(n):
        t = mean + amp * math.sin(2 * math.pi * (i + 0.5) / n)
        total += min(max(t, lo), hi) - lo
    return total / n


@pytest.mark.parametrize("tmin,tmax", [
    (5, 9),        # all below the base
    (15, 25),      # between the thresholds
    (8, 22),       # crosses the base
    (25, 41),      # crosses the upper threshold
    (6, 44),       # crosses both
    (39, 45),      # all above the upper threshold
])
def test_single_sine_matches_numerical_integration(tmin, tmax):
    lo, hi = 11.1, 37.8
    assert gdd_single_sine(tmin, tmax, lo, hi) == pytest.approx(_numeric_gdd(tmin, tmax, lo, hi), abs=0.01)


def test_thresholds_come_from_documented_values():
    assert PEPPER_T.t_base_c == pytest.approx((52 - 32) / 1.8, abs=0.05)    # 52 °F, Croptime
    assert PEPPER_T.t_upper_c == pytest.approx((100 - 32) / 1.8, abs=0.05)  # 100 °F, Croptime
    assert (POTATO_T.t_base_c, POTATO_T.t_upper_c) == (7.0, 30.0)
    assert CYCLES["tomato"].thermal is None and CYCLES["eggplant"].thermal is None   # not documented yet


def test_equivalent_normal_days():
    assert equivalent_normal_days(0, [5, 5]) == 0
    assert equivalent_normal_days(12.5, [5, 5, 5, 5]) == pytest.approx(2.5)
    assert equivalent_normal_days(100, [5, 5]) is None


# ------------------------------------------------------------------ thermal progress (pure)
def _archive(years, tmin, tmax, end_year):
    out = {}
    for y in range(end_year - years, end_year):
        d = date(y, 1, 1)
        while d.year == y:
            out[d] = DayTemp(tmin, tmax, "reanalysis", "test")
            d += timedelta(days=1)
    return out


def _season(start, n, tmin, tmax, kind="forecast"):
    return {start + timedelta(days=i): DayTemp(tmin, tmax, kind, "test") for i in range(n)}


START, TODAY = date(2026, 4, 1), date(2026, 5, 1)


@pytest.mark.parametrize("delta,direction", [(0, 0), (+4, +1), (-3, -1)])
def test_warmer_season_moves_the_crop_ahead(delta, direction):
    data = TemperatureData(archive=_archive(5, 14, 26, 2026), forecast=_season(START, 30, 14 + delta, 26 + delta))
    tp = thermal_progress(data, PEPPER_T, START, TODAY)
    assert tp["available"] and tp["coverage"] == 1.0
    diff = tp["equivalent_days"] - 30
    assert (diff > 1) if direction > 0 else (diff < -1) if direction < 0 else abs(diff) < 0.01


def test_no_normals_means_no_thermal_correction():
    data = TemperatureData(archive=_archive(2, 14, 26, 2026), forecast=_season(START, 30, 14, 26))
    tp = thermal_progress(data, PEPPER_T, START, TODAY)
    assert not tp["available"] and "normals" in tp["reason"]


def test_missing_days_count_as_normal_and_lower_the_coverage():
    data = TemperatureData(archive=_archive(5, 14, 26, 2026), forecast=_season(START, 20, 20, 32))
    tp = thermal_progress(data, PEPPER_T, START, TODAY)
    assert tp["missing_days"] == 10 and tp["coverage"] == pytest.approx(2 / 3, abs=0.01)
    assert not tp["available"] and "real temperature" in tp["reason"]


def test_measured_sensor_temperature_has_priority():
    data = TemperatureData(archive=_archive(5, 14, 26, 2026), forecast=_season(START, 30, 14, 26),
                           sensor=_season(START, 30, 18, 30, kind="measured"))
    tp = thermal_progress(data, PEPPER_T, START, TODAY)
    assert tp["days_by_source"] == {"measured": 30} and tp["equivalent_days"] > 30


# ------------------------------------------------------------------ stage content (pepper / potato)
REFS = Path(__file__).resolve().parent.parent / "docs" / "REFERENCES.md"


@pytest.mark.parametrize("crop", ["pepper", "potato"])
def test_every_stage_has_description_needs_and_documented_sources(crop):
    keys = set(re.findall(r"\*\*\[([A-Z0-9_]+)\]\*\*", REFS.read_text(encoding="utf-8")))
    for s in CYCLES[crop].stages:
        assert s.description_en and s.description_fr and s.needs and s.water_sensitivity
        for n in s.needs:
            assert n.text_en and n.text_fr and n.source in keys, (s.key, n.source)


def test_water_sensitive_stages_follow_fao():
    pep = {s.key: s.water_sensitivity for s in CYCLES["pepper"].stages}
    pot = {s.key: s.water_sensitivity for s in CYCLES["potato"].stages}
    assert pep["flowering"] == "critical"
    assert pot["tuber_initiation"] == pot["tuber_bulking"] == "critical"
    assert pot["sprouting_emergence"] == pot["maturation"] == "less_sensitive"
    assert any("25 %" in n.text_en for n in CYCLES["pepper"].get("flowering").needs)


# ------------------------------------------------------------------ integration with the API
def _seed_climate(session, today, tmin=14, tmax=26, years=5):
    rows = []
    for y in range(today.year - years, today.year):
        d = date(y, 1, 1)
        while d.year == y:
            rows.append(ClimateDay(day=d, tmin_c=tmin, tmax_c=tmax))
            d += timedelta(days=1)
    store_climate_days(session, rows)


def _seed_season(session, start, today, tmin, tmax):
    d = start
    while d <= today:
        session.add(WeatherDay(day=d, tmin_c=tmin, tmax_c=tmax, et0_mm=5.0, precip_mm=0, source="test"))
        d += timedelta(days=1)
    session.commit()


def _engine():
    from app.db import engine
    return engine


def _pepper_zone(session) -> Zone:
    return session.exec(select(Zone).where(Zone.name.contains("Pepper"))).one()


def test_stage_without_climate_data_is_calendar_only_and_says_why(client):
    st = client.get("/api/zones/2/stage").json()
    assert st["timing"]["used"] == "calendar"
    assert st["certainty"]["level"] in ("low", "medium")
    assert any("calendar only" in r for r in st["certainty"]["reasons"])
    # needs and description of the current stage are returned
    assert st["stage"]["description_fr"] and st["stage"]["needs"]
    # dates are consistent
    today = date.fromisoformat(st["today"])
    assert date.fromisoformat(st["stage_start_date"]) <= today < date.fromisoformat(st["next_stage_date"])
    assert (date.fromisoformat(st["next_stage_date"]) - today).days == st["days_to_next_stage"]
    assert 0 < st["cycle_progress_pct"] < 100


def test_warm_season_puts_the_pepper_ahead_of_the_calendar(client):
    with Session(_engine()) as s:
        zone = _pepper_zone(s)
        today = date.fromisoformat(client.get("/api/zones/2/stage").json()["today"])
        zone.planting_date = today - timedelta(days=55)        # calendar: day 55, vegetative (25-60)
        s.add(zone); s.commit()
        _seed_climate(s, today, 14, 26)
        _seed_season(s, zone.planting_date, today, 16, 28)     # 2 °C warmer than normal
    st = client.get("/api/zones/2/stage").json()
    t = st["timing"]
    assert t["used"] == "degree_days" and t["calendar_day"] == 55
    assert t["thermal_day"] > 60                               # ahead: already flowering
    assert st["stage"]["key"] == "flowering"
    assert t["possible_stages"] == ["vegetative", "flowering"]  # calendar and degree-days disagree
    assert st["certainty"]["level"] in ("low", "medium")
    assert any("differ" in r for r in st["certainty"]["reasons"])
    # the decision engine uses the corrected position
    d = client.post("/api/zones/2/decide").json()
    assert d["values"]["growth_stage"]["value"] == "flowering"
    assert d["values"]["effective_day"]["value"] == st["effective_day"]
    assert "degree-days" in d["values"]["days_to_next_stage"]["source"]
    # timeline: stage dates in order, and the thermal endpoint shows its sources
    starts = [r["estimated_start"] for r in st["timeline"]]
    assert starts == sorted(starts)
    th = client.get("/api/zones/2/thermal").json()
    assert th["available"] and th["days"][-1]["cum_gdd"] > th["days"][-1]["cum_normal_gdd"]
    assert {r["kind"] for r in th["days"]} == {"forecast"}


def test_normal_season_agrees_with_the_calendar(client):
    with Session(_engine()) as s:
        zone = _pepper_zone(s)
        today = date.fromisoformat(client.get("/api/zones/2/stage").json()["today"])
        zone.planting_date = today - timedelta(days=40)
        s.add(zone); s.commit()
        _seed_climate(s, today, 14, 26)
        _seed_season(s, zone.planting_date, today, 14, 26)
    st = client.get("/api/zones/2/stage").json()
    assert st["timing"]["thermal_day"] == pytest.approx(40, abs=0.1)
    assert st["stage"]["key"] == "vegetative"
    assert any("agree" in r for r in st["certainty"]["reasons"])


def test_farmer_confirmation_restarts_the_degree_day_count(client):
    with Session(_engine()) as s:
        zone = _pepper_zone(s)
        today = date.fromisoformat(client.get("/api/zones/2/stage").json()["today"])
        zone.planting_date = today - timedelta(days=50)
        s.add(zone); s.commit()
        _seed_climate(s, today, 14, 26)
        _seed_season(s, zone.planting_date, today, 14, 26)
    on = (today - timedelta(days=4)).isoformat()
    assert client.post("/api/zones/2/stage/confirm", json={"stage_key": "flowering", "observed_on": on}).status_code == 201
    st = client.get("/api/zones/2/stage").json()
    assert st["stage"]["key"] == "flowering" and st["stage_start_date"] == on
    assert st["timing"]["thermal_day"] == pytest.approx(60 + 4, abs=0.1)
    assert st["certainty"]["level"] == "confirmed"


def test_potato_underground_stage_still_needs_digging_with_degree_days(client):
    potato = next(c for c in client.get("/api/crops").json() if c["key"] == "potato")
    with Session(_engine()) as s:
        zone = _pepper_zone(s)
        today = date.fromisoformat(client.get("/api/zones/2/stage").json()["today"])
        zone.crop_profile_id, zone.planting_date = potato["id"], today - timedelta(days=62)
        s.add(zone); s.commit()
        _seed_climate(s, today, 10, 22)
        _seed_season(s, zone.planting_date, today, 10, 22)
    st = client.get("/api/zones/2/stage").json()
    assert st["timing"]["used"] == "degree_days"
    assert st["stage"]["key"] == "tuber_initiation" and st["field_check_due"]
    assert st["certainty"]["level"] == "low"
    assert st["stage"]["water_sensitivity"] == "critical"


def test_sensor_days_need_enough_readings(client):
    """A day of on-farm temperature counts only with readings spread over the day."""
    from app.climate import load_temperatures
    with Session(_engine()) as s:
        base = SensorReading(air_temp_c=20.0)
        day = base.ts.date() - timedelta(days=3)
        from datetime import datetime
        for h, t in ((1, 12.0), (7, 18.0), (13, 31.0), (19, 22.0)):
            s.add(SensorReading(ts=datetime.combine(day, datetime.min.time()) + timedelta(hours=h), air_temp_c=t))
        s.add(SensorReading(ts=datetime.combine(day - timedelta(days=1), datetime.min.time()), air_temp_c=25.0))
        s.commit()
        data = load_temperatures(s, day - timedelta(days=2), day + timedelta(days=1), tz="UTC")
    assert data.sensor[day].tmin == 12.0 and data.sensor[day].tmax == 31.0
    assert (day - timedelta(days=1)) not in data.sensor


# ------------------------------------------------------------------ archive download
def test_archive_parsing_and_refresh_with_mock_transport(client):
    payload = {"daily": {"time": ["2024-01-01", "2024-01-02", "2024-01-03"],
                         "temperature_2m_max": [16.0, None, 17.5], "temperature_2m_min": [8.0, 7.0, 9.0]}}
    assert [c.day.isoformat() for c in parse_archive(payload)] == ["2024-01-01", "2024-01-03"]
    seen = {}

    def handler(request: httpx.Request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=payload)

    with Session(_engine()) as s, httpx.Client(transport=httpx.MockTransport(handler)) as c:
        assert refresh_climate(s, years=5, today=date(2026, 11, 21), client=c) == 2
    assert seen["start_date"] == "2021-01-01" and seen["end_date"] == "2026-11-14"
    status = client.get("/api/climate/status").json()
    assert status["days"] == 2 and status["normals_available"] is False


# ------------------------------------------------------------------ reference season (FAO planting window)
def _seasonal_archive(years, end_year):
    """Mediterranean-like seasons: cool January, hot August (synthetic, for the test only)."""
    import math as _m
    out = {}
    for y in range(end_year - years, end_year):
        d = date(y, 1, 1)
        while d.year == y:
            mean = 18.5 + 8.5 * _m.sin(2 * _m.pi * (d.timetuple().tm_yday - 120) / 365)
            out[d] = DayTemp(mean - 5, mean + 5, "reanalysis", "test")
            d += timedelta(days=1)
    return out


def test_winter_pepper_is_behind_the_fao_calendar():
    """FAO pepper lengths are for an April-June planting: a pepper planted on 1 December in a normal
    winter accumulates far fewer degree-days than in the reference season, so it is behind the calendar."""
    from app.climate import thermal_progress
    archive = _seasonal_archive(5, 2026)
    start, today = date(2025, 12, 1), date(2026, 1, 30)
    season = {start + timedelta(days=i): archive.get(date(2024, (start + timedelta(days=i)).month,
                                                          (start + timedelta(days=i)).day))
              for i in range((today - start).days)}
    data = TemperatureData(archive={d: t for d, t in archive.items() if d.year < 2025}, forecast=season)
    same_dates = thermal_progress(data, PEPPER_T, start, today)
    reference = thermal_progress(data, PEPPER_T, start, today, norm_start=date(2025, 5, 15))
    assert same_dates["equivalent_days"] == pytest.approx(60, abs=2)      # normal winter vs normal winter (leap-year shift)
    assert reference["equivalent_days"] < 30                              # far behind the 60 calendar days
    assert reference["reference_gdd"] > reference["actual_gdd"]


def test_cycles_declare_their_reference_planting():
    assert CYCLES["pepper"].reference_planting == (5, 15)
    assert CYCLES["potato"].reference_planting == (1, 15)
    assert CYCLES["pepper"].get("flowering").max_depletion == 0.25
