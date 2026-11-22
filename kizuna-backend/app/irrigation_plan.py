"""Irrigation plan for the coming days: frequency, next irrigation, volume, duration, weekly water.

Nothing here is a fixed rule. The plan is a day-by-day projection of the SAME FAO-56 root-zone
water balance used by the decision engine (decision.py):

    start      : depletion Dr computed from the measured soil moisture (now)
    each day   : Dr += ETc - effective rain        ETc = Kc(stage of that day) x ET0(forecast)
    irrigation : when Dr would pass RAW = p x TAW during the day -> first irrigation window that day,
                 refill towards field capacity (zone refill fraction), gross = net / efficiency
    p          : FAO-56 Table 22 adjusted for ETc, limited by the stage's documented maximum
                 depletion (e.g. 25 % at the start of pepper flowering, FAO crop information)
    Kc, roots  : follow the crop's stage on each future day (calendar pace from today's position)

Frequency, irrigations per week, volume and duration are read from the projected events, so they
change with the stage, the soil, the root depth, the weather forecast, the rain and the efficiency.
When an input is missing, the plan says which one instead of inventing a value.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from . import growth
from .agronomy import kc as kcmod
from .agronomy import water_balance as wb
from .config import Settings
from .config import settings as default_settings
from .decision import decide
from .models import CropProfile, WeatherDay, Zone, utcnow

HORIZON_DAYS = 7
FORECAST_MAX_AGE_H = 24

STATUS = {"irrigate_now": "irrigation_needed", "schedule": "irrigation_needed", "no_irrigation": "waiting",
          "postpone_rain": "postponed_rain", "blocked": "blocked", "fixed_schedule": "fixed_schedule"}
STATUS_TEXT = {
    "irrigation_needed": ("Irrigation needed", "Irrigation nécessaire"),
    "waiting": ("Waiting: soil water still sufficient", "Attente : réserve en eau suffisante"),
    "postponed_rain": ("Irrigation postponed for rain", "Irrigation reportée (pluie)"),
    "blocked": ("Automatic irrigation blocked", "Irrigation automatique bloquée"),
    "irrigating": ("Irrigation in progress", "Irrigation en cours"),
    "fixed_schedule": ("Fixed schedule (control zone)", "Calendrier fixe (zone témoin)"),
    "insufficient_data": ("Not enough data for a recommendation", "Données insuffisantes pour recommander"),
}


def _when_text(dt: datetime, now_local: datetime) -> tuple[str, str]:
    days = (dt.date() - now_local.date()).days
    hhmm_en, hhmm_fr = dt.strftime("%H:%M"), dt.strftime("%H h %M")
    if days == 0:
        return f"today at {hhmm_en}", f"aujourd'hui à {hhmm_fr}"
    if days == 1:
        return f"tomorrow at {hhmm_en}", f"demain à {hhmm_fr}"
    return f"{dt:%d/%m} at {hhmm_en}", f"le {dt:%d/%m} à {hhmm_fr}"


def _frequency_text(interval: float | None) -> tuple[str, str]:
    if interval is None:
        return "less than once a week", "moins d'une fois par semaine"
    if interval < 1.5:
        return "every day", "tous les jours"
    n = round(interval)
    return f"every {n} days", f"tous les {n} jours"


def _daily_et0(session: Session, today: date, horizon: int, weather_today, now: datetime) -> tuple[list[dict], list[str]]:
    """ET0 / rain for each day of the horizon, with the source of each value."""
    rows = {w.day: w for w in session.exec(select(WeatherDay).where(
        WeatherDay.day >= today, WeatherDay.day < today + timedelta(days=horizon))).all()}
    out, notes = [], []
    for i in range(horizon):
        d = today + timedelta(days=i)
        w = rows.get(d)
        fresh = w is not None and w.et0_mm is not None and (now - w.fetched_at) <= timedelta(hours=FORECAST_MAX_AGE_H)
        if fresh:
            out.append({"day": d, "et0": w.et0_mm, "et0_kind": "forecast", "precip": w.precip_mm or 0.0,
                        "precip_prob": w.precip_probability_pct, "rain_known": True,
                        "u2": w.u2_ms, "rh_min": w.rh_min_pct})
        elif weather_today is not None:
            out.append({"day": d, "et0": weather_today.et0_mm, "et0_kind": "persistence", "precip": 0.0,
                        "precip_prob": None, "rain_known": False, "u2": weather_today.u2_ms,
                        "rh_min": weather_today.rh_min_pct})
        else:
            out.append(None)
    n_pers = sum(1 for o in out if o and o["et0_kind"] == "persistence")
    if n_pers:
        notes.append(f"{n_pers} day(s) without forecast: ET0 assumed equal to today's ({weather_today.et0_source}); "
                     "rain unknown on those days.")
    return out, notes


def build_plan(session: Session, zone: Zone, now: datetime | None = None, cfg: Settings = default_settings,
               horizon_days: int = HORIZON_DAYS) -> dict[str, Any]:
    from .services import build_inputs, local_today   # local import: services imports this module's callers

    now = now or utcnow()
    inp = build_inputs(session, zone, now, cfg)
    tz = ZoneInfo(cfg.timezone)
    now_local = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    today = local_today(now, cfg)
    si = inp.stage_info if inp.stage_info and inp.stage_info.get("available") else None
    decision = decide(inp)
    status = STATUS.get(decision.action, "waiting")
    from .services import active_command
    running = active_command(session, zone.id)
    if running is not None and running.status in ("sent", "running"):
        status = "irrigating"

    plan: dict[str, Any] = {"zone_id": zone.id, "zone_name": zone.name, "generated_at": now_local.isoformat(),
                            "horizon_days": horizon_days, "data_kind": "real", "missing": [], "assumptions": [],
                            "decision_action": decision.action, "decision_reasons": decision.reasons}
    missing = plan["missing"]
    if inp.crop is None:
        missing.append("crop of the zone")
    if inp.planting_date is None:
        missing.append("planting date")
    days_w, notes = _daily_et0(session, today, horizon_days, inp.weather_today, now)
    plan["assumptions"] += notes
    if any(d is None for d in days_w):
        missing.append("ET0: no weather forecast and no air temperature reading")
    if missing:
        plan.update(status="insufficient_data", status_text_en=STATUS_TEXT["insufficient_data"][0],
                    status_text_fr=STATUS_TEXT["insufficient_data"][1])
        return plan

    profile = session.get(CropProfile, zone.crop_profile_id)
    cycle = growth.cycle_of(profile)
    crop = inp.crop
    start_day = si["effective_day"] if si else (today - inp.planting_date).days

    # starting depletion: measured soil moisture, or "unknown"
    soil_ok = (inp.soil_theta is not None and not inp.soil_reading_issues and inp.soil_reading_age_min is not None
               and inp.soil_reading_age_min <= inp.reading_max_age_min)
    zr0 = inp.root_depth_override_m or kcmod.root_depth_for_day(crop.zr_ini_m, crop.zr_max_m, crop.curve, start_day)
    taw0 = wb.total_available_water(inp.theta_fc, inp.theta_wp, zr0)
    if soil_ok:
        dr = wb.depletion_from_soil_moisture(inp.soil_theta, inp.theta_fc, zr0, taw0)
        plan["start_depletion"] = {"value": round(dr, 2), "unit": "mm", "kind": "calculated",
                                   "source": "from the measured soil moisture"}
    else:
        dr = 0.0
        missing.append("valid, recent soil moisture reading (needed for the date of the next irrigation)")
        plan["assumptions"].append("Without a soil moisture reading, the frequency is computed as if the root zone "
                                   "were refilled today; the next irrigation date cannot be given.")

    windows = inp.irrigation_windows
    rain_rule = cfg.rain_postpone_probability_pct
    plan["assumptions"].append(f"Forecast rain is counted only when its probability is ≥ {rain_rule:.0f} % "
                               "(same rule as the rain postponement); rain below 0.2 ET0 is ignored (FAO-56).")
    plan["assumptions"].append("Future stages follow the calendar pace from today's estimated position.")

    # ---- per-day agronomic values (stage, Kc, ETc, roots, TAW, RAW, effective rain)
    days = []
    for i, w in enumerate(days_w):
        eday = start_day + i
        stage = growth.calendar_position(cycle, eday)["stage"]
        kc_mid, kc_end = crop.curve.kc_mid, crop.curve.kc_end
        if w["u2"] is not None and w["rh_min"] is not None:
            kc_mid = kcmod.adjust_kc_mid(crop.curve.kc_mid, w["u2"], w["rh_min"], crop.max_height_m)
            kc_end = kcmod.adjust_kc_end(crop.curve.kc_end, w["u2"], w["rh_min"], crop.max_height_m)
        kc = kcmod.kc_for_day(crop.curve, eday, kc_mid, kc_end)
        etc = kc * w["et0"]
        zr = inp.root_depth_override_m or kcmod.root_depth_for_day(crop.zr_ini_m, crop.zr_max_m, crop.curve, eday)
        taw = wb.total_available_water(inp.theta_fc, inp.theta_wp, zr)
        p = wb.adjusted_depletion_fraction(crop.p, etc)
        if stage.max_depletion is not None and stage.max_depletion < p:
            p = stage.max_depletion
        prob = w["precip_prob"]
        counted = w["rain_known"] and (prob is None or prob >= rain_rule)
        peff = wb.effective_rainfall(w["precip"], w["et0"]) if counted else 0.0
        days.append({"w": w, "stage": stage, "kc": kc, "etc": etc, "zr": zr, "taw": taw, "p": p,
                     "raw": wb.readily_available_water(p, taw), "peff": peff, "events": [], "postponed": False,
                     "dr_start": None, "dr_end": None})

    # ---- replay of the decision rule every STEP minutes (the scheduler decides every 15 min)
    step = timedelta(minutes=15)
    end_local = datetime.combine(today + timedelta(days=horizon_days), time(0), tzinfo=tz)
    t = now_local
    events = []

    def in_window(x: datetime) -> bool:
        return any(a <= x.time() < b for a, b in windows)

    def next_window_start(x: datetime) -> datetime | None:
        cands = [datetime.combine(x.date() + timedelta(days=k), a, tzinfo=tz) for k in (0, 1, 2) for a, _ in windows]
        cands = [c for c in cands if c > x]
        return min(cands) if cands else None

    while t < end_local:
        i = (t.date() - today).days
        day = days[i]
        if day["dr_start"] is None:
            day["dr_start"] = dr
        raw, taw = day["raw"], day["taw"]
        if dr > raw:
            irrigate = False
            if in_window(t):
                irrigate = True
                nxt_day = days[i + 1] if i + 1 < len(days) else None
                if nxt_day is not None and nxt_day["peff"] > 0:
                    net_needed = wb.net_irrigation_mm(dr, inp.refill_fraction)
                    if nxt_day["peff"] >= net_needed and dr + day["etc"] < taw:
                        irrigate = False                       # same rule as decision.py: postpone for rain
                        day["postponed"] = True
            else:
                nws = next_window_start(t)
                if nws is not None and dr + day["etc"] * (nws - t).total_seconds() / 86400 >= taw:
                    irrigate = True                            # waiting would exhaust the available water
            if irrigate:
                net = wb.net_irrigation_mm(dr, inp.refill_fraction)
                volume = wb.mm_to_litres(wb.gross_irrigation_mm(net, inp.efficiency), inp.area_m2)
                capped = volume > inp.max_volume_per_event_l
                volume = min(volume, inp.max_volume_per_event_l)
                net_applied = wb.litres_to_mm(volume, inp.area_m2) * inp.efficiency
                duration = volume / inp.flow_rate_lpm if inp.flow_rate_lpm > 0 else None
                en, fr = _when_text(t, now_local)
                ev = {"at": t.isoformat(timespec="minutes"), "when_en": en, "when_fr": fr, "stage": day["stage"].key,
                      "depletion_mm": round(dr, 1), "raw_mm": round(raw, 1),
                      "net_mm": round(net_applied, 2), "gross_mm": round(net_applied / inp.efficiency, 2),
                      "volume_l": round(volume, 1), "duration_min": round(duration, 1) if duration else None,
                      "capped_to_safety_max": capped, "kind": "calculated"}
                events.append(ev)
                day["events"].append(ev["at"])
                dr = max(dr - net_applied, 0.0)
        nt = t + step
        frac = ((nt.replace(minute=(nt.minute // 15) * 15, second=0, microsecond=0)) - t).total_seconds() / 86400
        dr = min(max(dr + (day["etc"] - day["peff"]) * frac, 0.0), taw)
        day["dr_end"] = dr
        nt = t + step
        t = nt.replace(minute=(nt.minute // 15) * 15, second=0, microsecond=0)   # quarter-hour checks

    table = []
    for day in days:
        w = day["w"]
        table.append({"day": w["day"].isoformat(), "stage": day["stage"].key, "kc": round(day["kc"], 3),
                      "et0_mm": round(w["et0"], 2), "et0_kind": w["et0_kind"], "etc_mm": round(day["etc"], 2),
                      "root_depth_m": round(day["zr"], 3), "taw_mm": round(day["taw"], 1), "p": round(day["p"], 3),
                      "raw_mm": round(day["raw"], 1), "rain_mm": w["precip"],
                      "rain_probability_pct": w["precip_prob"], "effective_rain_mm": round(day["peff"], 2),
                      "depletion_start_mm": round(day["dr_start"] or 0, 1),
                      "depletion_end_mm": round(day["dr_end"] or 0, 1),
                      "irrigations": day["events"], "postponed_for_rain": day["postponed"]})

    # ---- summary
    scale = 7 / horizon_days
    etc_mean = sum(r["etc_mm"] for r in table) / len(table)
    raw_mean = sum(r["raw_mm"] for r in table) / len(table)
    if len(events) >= 2:
        ts = [datetime.fromisoformat(e["at"]) for e in events]
        interval = sum((b - a).total_seconds() for a, b in zip(ts, ts[1:])) / (len(ts) - 1) / 86400
        interval_basis = "mean gap between the projected irrigations"
    else:
        interval = raw_mean / etc_mean if etc_mean > 0 else None
        interval_basis = "RAW / ETc (time for the crop to use the readily available water)"
    if interval is not None and interval > 7 * 4:
        interval = None
    freq_en, freq_fr = _frequency_text(interval)
    if events:
        vol = sum(e["volume_l"] for e in events) / len(events)
        vol_basis = "mean of the projected irrigations"
    else:
        vol = wb.mm_to_litres(wb.gross_irrigation_mm(raw_mean * inp.refill_fraction, inp.efficiency), inp.area_m2)
        vol = min(vol, inp.max_volume_per_event_l)
        vol_basis = "refill of the readily available water (RAW / efficiency)"
    weekly_l = sum(e["volume_l"] for e in events) * scale
    demand_l = wb.mm_to_litres(sum(max(r["etc_mm"] - r["effective_rain_mm"], 0) for r in table) / inp.efficiency,
                               inp.area_m2) * scale

    nxt = events[0] if (events and soil_ok) else None
    if status in ("blocked", "irrigating"):
        nxt = None
    if status == "irrigating":
        plan_note = ("An irrigation is running now; the next one is computed after it ends "
                     "(the soil moisture used here is from before the water arrived).")
        plan["assumptions"].append(plan_note)
    plan.update({
        "status": status, "status_text_en": STATUS_TEXT[status][0], "status_text_fr": STATUS_TEXT[status][1],
        "crop_key": profile.key, "crop_name": profile.name,
        "stage": {"key": si["stage"]["key"], "name_en": si["stage"]["name_en"], "name_fr": si["stage"]["name_fr"],
                  "certainty": si["certainty"]["level"]} if si else None,
        "next_stage": ({"key": si["next_stage"]["key"], "name_en": si["next_stage"]["name_en"],
                        "name_fr": si["next_stage"]["name_fr"], "date": si["next_stage_date"],
                        "in_days": si["days_to_next_stage"], "basis": si["next_stage_basis"]}
                       if si and si.get("next_stage") else None),
        "frequency": {"interval_days": round(interval, 1) if interval else None, "text_en": freq_en,
                      "text_fr": freq_fr, "basis": interval_basis, "kind": "calculated"},
        "irrigations_per_week": round(len(events) * scale, 1),
        "volume_per_irrigation_l": round(vol, 1), "volume_basis": vol_basis,
        "duration_per_irrigation_min": round(vol / inp.flow_rate_lpm, 1) if inp.flow_rate_lpm > 0 else None,
        "duration_basis": f"volume / nominal flow {inp.flow_rate_lpm} L/min",
        "weekly_water_l": round(weekly_l, 1),
        "weekly_crop_demand_l": round(demand_l, 1),
        "weekly_note": "weekly_water_l = projected irrigations; weekly_crop_demand_l = (ETc - effective rain) / "
                       "efficiency over the week. They differ when the soil reserve covers part of the week.",
        "next_irrigation": nxt,
        "events": events,
        "daily": table,
        "inputs": {"soil": inp.soil_source, "theta_fc": inp.theta_fc, "theta_wp": inp.theta_wp,
                   "efficiency": inp.efficiency, "efficiency_source": inp.efficiency_source,
                   "area_m2": inp.area_m2, "flow_rate_lpm": inp.flow_rate_lpm, "refill_fraction": inp.refill_fraction,
                   "irrigation_windows": [f"{a:%H:%M}-{b:%H:%M}" for a, b in windows]},
    })
    if status == "blocked":
        plan["assumptions"].append("Automatic irrigation is blocked (see decision_reasons): the schedule shows what "
                                   "the crop would need, nothing will be sent to the valves.")
    return plan


def plan_summary(plan: dict[str, Any]) -> dict[str, Any]:
    """Short version for the photo-analysis answer."""
    keys = ("status", "status_text_en", "status_text_fr", "frequency", "irrigations_per_week",
            "volume_per_irrigation_l", "duration_per_irrigation_min", "weekly_water_l", "next_irrigation",
            "next_stage", "missing")
    out = {k: plan.get(k) for k in keys}
    if out.get("next_irrigation"):
        out["next_irrigation"] = {k: plan["next_irrigation"][k] for k in ("at", "when_en", "when_fr", "volume_l",
                                                                          "duration_min")}
    return out
