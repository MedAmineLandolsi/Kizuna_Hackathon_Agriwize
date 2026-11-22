"""Irrigation decision engine.

Quantities come only from the FAO-56 water balance (agronomic calculation)
fed by measured soil moisture and weather data. AI is used only to identify
the crop (and that identification must be confirmed when confidence is low).
Safety rules can block any decision.

Every value in the output carries a provenance label:
  measured | calculated | forecast | reference | configured | ai_prediction
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .agronomy import kc as kcmod
from .agronomy import water_balance as wb
from .weather import WeatherInputs

ACTIONS = ("irrigate_now", "schedule", "no_irrigation", "postpone_rain", "blocked", "fixed_schedule")


@dataclass
class CropInputs:
    name: str
    curve: kcmod.KcCurve
    max_height_m: float
    zr_ini_m: float
    zr_max_m: float
    p: float
    source: str


@dataclass
class DecisionInputs:
    now_utc: datetime                      # naive UTC
    timezone: str
    zone_name: str
    zone_enabled: bool
    strategy: str
    planting_date: date | None
    crop: CropInputs | None
    identification_status: str
    theta_fc: float
    theta_wp: float
    soil_source: str
    root_depth_override_m: float | None
    area_m2: float
    efficiency: float
    efficiency_source: str
    flow_rate_lpm: float
    refill_fraction: float
    # measurements
    soil_theta: float | None
    soil_reading_age_min: float | None
    soil_reading_issues: list[str]
    tank_level_pct: float | None
    # weather
    weather_today: WeatherInputs | None
    rain_tomorrow_mm: float | None
    rain_tomorrow_probability_pct: float | None
    # system / safety
    mode: str
    emergency_stop: bool
    blocking_anomalies: list[str]
    active_command: bool
    # configured rules
    irrigation_windows: list[tuple[time, time]]
    rain_postpone_probability_pct: float
    reading_max_age_min: float
    min_tank_level_pct: float
    max_volume_per_event_l: float
    timeout_factor: float
    # rain observed right now (rain sensor or camera), not forecast
    raining_now: bool = False
    raining_now_source: str = ""
    # crop-specific growth stage (growth.assess); effective_day shifts the FAO curve when the farmer
    # confirmed a stage earlier/later than the calendar
    effective_day: int | None = None
    stage_info: dict | None = None


@dataclass
class DecisionResult:
    action: str
    reasons: list[str] = field(default_factory=list)
    values: dict[str, Any] = field(default_factory=dict)
    target_volume_l: float | None = None
    scheduled_for_utc: datetime | None = None
    timeout_s: int | None = None


def _v(value: Any, unit: str, kind: str, source: str = "") -> dict[str, Any]:
    if isinstance(value, float):
        value = round(value, 4)
    return {"value": value, "unit": unit, "kind": kind, "source": source}


def _to_local(now_utc: datetime, tz: str) -> datetime:
    return now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz))


def _to_utc_naive(local_dt: datetime) -> datetime:
    return local_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def in_window(local_now: datetime, windows: list[tuple[time, time]]) -> bool:
    t = local_now.time()
    return any(start <= t < end for start, end in windows)


def next_window_start(local_now: datetime, windows: list[tuple[time, time]]) -> datetime:
    candidates = []
    for offset in (0, 1):
        d = local_now.date() + timedelta(days=offset)
        for start, _ in windows:
            dt = datetime.combine(d, start, tzinfo=local_now.tzinfo)
            if dt > local_now:
                candidates.append(dt)
    return min(candidates)


def decide(inp: DecisionInputs) -> DecisionResult:
    res = DecisionResult(action="no_irrigation")
    vals = res.values
    local_now = _to_local(inp.now_utc, inp.timezone)

    # ---------- 1. Safety gates ----------
    blocks: list[str] = []
    if inp.emergency_stop:
        blocks.append("Emergency stop is active.")
    if inp.mode != "auto" and inp.strategy == "adaptive":
        blocks.append("System in manual mode: automatic irrigation disabled.")
    if not inp.zone_enabled:
        blocks.append("Zone disabled.")
    if inp.crop is None:
        blocks.append("No crop profile assigned to this zone.")
    if inp.identification_status != "confirmed":
        blocks.append("Crop identification not confirmed - farmer confirmation required.")
    if inp.planting_date is None:
        blocks.append("Planting date missing.")
    if inp.active_command:
        blocks.append("An irrigation command is already pending or running for this zone.")
    blocks.extend(f"Open anomaly: {a}" for a in inp.blocking_anomalies)
    if inp.tank_level_pct is not None and inp.tank_level_pct < inp.min_tank_level_pct:
        blocks.append(f"Water tank level {inp.tank_level_pct:.0f}% below minimum {inp.min_tank_level_pct:.0f}%.")
    if inp.soil_theta is None:
        blocks.append("No soil moisture reading.")
    elif inp.soil_reading_issues:
        blocks.append("Invalid soil moisture reading: " + "; ".join(inp.soil_reading_issues))
    elif inp.soil_reading_age_min is None or inp.soil_reading_age_min > inp.reading_max_age_min:
        blocks.append(f"Soil moisture reading older than {inp.reading_max_age_min:.0f} min.")

    if inp.tank_level_pct is not None:
        vals["tank_level"] = _v(inp.tank_level_pct, "%", "measured", "tank level sensor")
    if inp.soil_theta is not None:
        vals["soil_moisture"] = _v(inp.soil_theta, "m3/m3", "measured", "capacitive sensor (calibrated)")

    # ---------- 2. Agronomic calculation (done even when blocked, for the dashboard) ----------
    can_compute = inp.crop is not None and inp.planting_date is not None
    etc = None
    if can_compute:
        crop = inp.crop
        dap_calendar = (local_now.date() - inp.planting_date).days
        dap = inp.effective_day if inp.effective_day is not None else dap_calendar
        fao = kcmod.stage_for_day(crop.curve, dap)
        vals["days_after_planting"] = _v(dap_calendar, "days", "calculated", "planting date")
        if inp.effective_day is not None and inp.effective_day != dap_calendar:
            vals["effective_day"] = _v(dap, "days", "calculated", "shifted by the farmer's stage confirmation")
        si = inp.stage_info
        if si and si.get("available"):
            kind = "confirmed" if si.get("kind") == "confirmed" else "calculated"
            vals["growth_stage"] = _v(si["stage"]["key"], "", kind,
                                      f"{si['crop_name']} cycle: {si['stage']['name_en']} ({si['confidence_label']})")
            vals["growth_stage_name"] = _v(si["stage"]["name_en"], "", kind, si["stage"]["bbch"])
            timing = (si.get("timing") or {}).get("used", "calendar")
            timing_src = "crop cycle + degree-days of the season" if timing == "degree_days" else "crop cycle (calendar)"
            vals["days_to_next_stage"] = _v(si.get("days_to_next_stage"), "days", "calculated", timing_src)
            if si.get("stage_start_date"):
                vals["stage_start_date"] = _v(si["stage_start_date"], "date", kind, si.get("stage_start_basis", ""))
            if si.get("next_stage_date"):
                vals["next_stage_date"] = _v(si["next_stage_date"], "date", "calculated",
                                             (si.get("next_stage_basis") or "") + " – estimate, not a certainty")
            if si.get("certainty"):
                vals["stage_certainty"] = _v(si["certainty"]["level"], "", "calculated",
                                             "; ".join(si["certainty"]["reasons"]))
            if si.get("needs_confirmation"):
                res.reasons.append("Growth stage to confirm: " + " ".join(si.get("reasons", [])))
        else:
            vals["growth_stage"] = _v(fao.stage, "", "calculated", "FAO-56 stage lengths from crop profile")
            vals["days_to_next_stage"] = _v(fao.days_to_next_stage, "days", "calculated", "crop profile")
        vals["fao_period"] = _v(fao.stage, "", "calculated", "FAO-56 Kc period used for this day")

        w = inp.weather_today
        kc_mid, kc_end = crop.curve.kc_mid, crop.curve.kc_end
        kc_note = "table values (no climate adjustment)"
        if w and w.u2_ms is not None and w.rh_min_pct is not None:
            kc_mid = kcmod.adjust_kc_mid(crop.curve.kc_mid, w.u2_ms, w.rh_min_pct, crop.max_height_m)
            kc_end = kcmod.adjust_kc_end(crop.curve.kc_end, w.u2_ms, w.rh_min_pct, crop.max_height_m)
            kc_note = "FAO-56 Eq. 62/65 climate adjustment (u2, RHmin)"
        kc_today = kcmod.kc_for_day(crop.curve, dap, kc_mid, kc_end)
        vals["kc"] = _v(kc_today, "", "calculated", f"{crop.source}; {kc_note}")

        if w:
            vals["et0"] = _v(w.et0_mm, "mm/day", w.et0_kind, w.et0_source)
            etc = kc_today * w.et0_mm
            vals["etc"] = _v(etc, "mm/day", "calculated", "ETc = Kc x ET0 (FAO-56 Eq. 56)")
        else:
            res.reasons.append("ET0 unavailable (no forecast, no local temperature): p not adjusted, no timing projection.")

        zr = inp.root_depth_override_m or kcmod.root_depth_for_day(crop.zr_ini_m, crop.zr_max_m, crop.curve, dap)
        vals["root_depth"] = _v(zr, "m", "configured" if inp.root_depth_override_m else "calculated",
                                "field value" if inp.root_depth_override_m else "linear root growth to Zr max")
        vals["theta_fc"] = _v(inp.theta_fc, "m3/m3", "reference", inp.soil_source)
        vals["theta_wp"] = _v(inp.theta_wp, "m3/m3", "reference", inp.soil_source)
        taw = wb.total_available_water(inp.theta_fc, inp.theta_wp, zr)
        p = wb.adjusted_depletion_fraction(crop.p, etc) if etc is not None else crop.p
        p_src = "FAO-56 Table 22, adjusted for ETc"
        cap = ((si or {}).get("stage") or {}).get("max_depletion") if si and si.get("available") else None
        if cap is not None and cap < p:
            p = cap
            p_src = f"stage limit {cap:.2f} ({si['stage']['name_en']}, {si['stage'].get('max_depletion_source')})"
        raw = wb.readily_available_water(p, taw)
        vals["taw"] = _v(taw, "mm", "calculated", "FAO-56 Eq. 82")
        vals["p"] = _v(p, "", "calculated", p_src)
        vals["raw"] = _v(raw, "mm", "calculated", "FAO-56 Eq. 83")

        if inp.soil_theta is not None and not inp.soil_reading_issues:
            dr = wb.depletion_from_soil_moisture(inp.soil_theta, inp.theta_fc, zr, taw)
            ks = wb.water_stress_coefficient(dr, taw, raw, p)
            vals["depletion"] = _v(dr, "mm", "calculated", "from measured soil moisture")
            vals["ks"] = _v(ks, "", "calculated", "FAO-56 Eq. 84")
            net = wb.net_irrigation_mm(dr, inp.refill_fraction)
            gross = wb.gross_irrigation_mm(net, inp.efficiency)
            volume = wb.mm_to_litres(gross, inp.area_m2)
            vals["net_irrigation"] = _v(net, "mm", "calculated", "refill root zone towards field capacity")
            vals["efficiency"] = _v(inp.efficiency, "", "configured", inp.efficiency_source)
            vals["gross_irrigation"] = _v(gross, "mm", "calculated", "net / efficiency")
            vals["required_volume"] = _v(volume, "L", "calculated", f"gross depth x {inp.area_m2} m2")
        else:
            dr = None
    else:
        dr = None

    if inp.strategy == "fixed":
        res.action = "fixed_schedule"
        res.reasons.append("Control zone: irrigated on a fixed schedule for the comparison experiment.")
        return res

    if blocks:
        res.action = "blocked"
        res.reasons = blocks + res.reasons
        return res

    # From here: all safety gates passed, dr is known.
    assert dr is not None
    taw = vals["taw"]["value"]
    raw = vals["raw"]["value"]

    if dr <= raw:
        res.action = "no_irrigation"
        msg = f"Depletion {dr:.1f} mm is within readily available water ({raw:.1f} mm): no stress, no irrigation."
        if etc and etc > 0:
            days = (raw - dr) / etc
            vals["days_until_irrigation"] = _v(days, "days", "calculated", "(RAW - Dr) / ETc")
            msg += f" Next irrigation expected in about {days:.1f} days."
        res.reasons.append(msg)
        return res

    # ---------- 3a. Rain observed now ----------
    if inp.raining_now:
        vals["raining_now"] = _v(True, "", "measured" if "sensor" in inp.raining_now_source else "ai_prediction",
                                 inp.raining_now_source)
        res.action = "postpone_rain"
        res.reasons.append(f"Rain observed now ({inp.raining_now_source}). Irrigation postponed; "
                           "soil moisture is re-measured at the next cycle.")
        return res
    net = vals["net_irrigation"]["value"]
    volume = vals["required_volume"]["value"]
    if volume > inp.max_volume_per_event_l:
        res.reasons.append(f"Volume capped from {volume:.1f} L to the safety maximum {inp.max_volume_per_event_l:.0f} L per event.")
        volume = inp.max_volume_per_event_l

    # ---------- 3. Rain postponement ----------
    w = inp.weather_today
    if w and w.rain_known and inp.rain_tomorrow_mm is not None:
        et0_ref = w.et0_mm
        peff = wb.effective_rainfall(inp.rain_tomorrow_mm, et0_ref)
        vals["rain_forecast_24h"] = _v(inp.rain_tomorrow_mm, "mm", "forecast", "Open-Meteo")
        vals["rain_probability_24h"] = _v(inp.rain_tomorrow_probability_pct, "%", "forecast", "Open-Meteo")
        vals["effective_rain_24h"] = _v(peff, "mm", "calculated", "FAO-56: rain < 0.2 ET0 ignored")
        prob = inp.rain_tomorrow_probability_pct or 0.0
        survives = etc is None or dr + etc < taw
        if prob >= inp.rain_postpone_probability_pct and peff >= net and survives:
            res.action = "postpone_rain"
            res.reasons.append(
                f"Rain expected in the next 24 h ({inp.rain_tomorrow_mm:.1f} mm, {prob:.0f}% probability) "
                f"covers the net requirement ({net:.1f} mm). Irrigation postponed; re-evaluated at the next cycle.")
            return res
    elif w and not w.rain_known:
        res.reasons.append("Rain forecast unavailable (offline): rain not taken into account.")

    # ---------- 4. Timing ----------
    duration_min = volume / inp.flow_rate_lpm if inp.flow_rate_lpm > 0 else 0.0
    res.target_volume_l = round(volume, 2)
    res.timeout_s = int(max(duration_min * 60 * inp.timeout_factor, 60))
    vals["target_volume"] = _v(volume, "L", "calculated", "after safety cap")
    vals["expected_duration"] = _v(duration_min, "min", "calculated", f"volume / nominal flow {inp.flow_rate_lpm} L/min")

    if in_window(local_now, inp.irrigation_windows):
        res.action = "irrigate_now"
        res.scheduled_for_utc = inp.now_utc
        res.reasons.append(f"Depletion {dr:.1f} mm exceeds RAW {raw:.1f} mm and we are inside an irrigation window.")
        return res

    start_local = next_window_start(local_now, inp.irrigation_windows)
    hours = (start_local - local_now).total_seconds() / 3600.0
    projected = dr + (etc or 0.0) * hours / 24.0
    vals["projected_depletion_at_window"] = _v(projected, "mm", "calculated", "Dr + ETc x hours / 24")
    if projected >= taw:
        res.action = "irrigate_now"
        res.scheduled_for_utc = inp.now_utc
        res.reasons.append(
            f"Waiting {hours:.1f} h for the next window would exhaust the available water "
            f"(projected {projected:.1f} mm >= TAW {taw:.1f} mm): irrigating now.")
        return res

    res.action = "schedule"
    res.scheduled_for_utc = _to_utc_naive(start_local)
    res.reasons.append(
        f"Depletion {dr:.1f} mm exceeds RAW {raw:.1f} mm. Scheduled for the next irrigation window "
        f"({start_local:%Y-%m-%d %H:%M} local) to reduce evaporation losses.")
    return res
