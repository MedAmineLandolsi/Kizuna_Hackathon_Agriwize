"""Growth-stage engine: which stage is a zone or a plant in, and why.

Evidence, strongest first:
1. Farmer confirmation (or correction), dated  -> anchors the cycle on that date.
2. Timing: days since planting (or since the confirmation) through the crop's OWN cycle
   (crop_cycles.py), corrected by degree-days when the temperatures and the local climate normals
   are available (climate.py): a warmer-than-normal season moves the crop ahead of the calendar.
3. AI photo: a visual class, compatible with one or more stages of that crop.

The AI never sets a stage on its own. It confirms the calendar, or asks the farmer.
Underground stages (potato tuber initiation / bulking) can never be confirmed by a photo:
the farmer is asked to dig one plant.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from .agronomy.crop_cycles import CropCycle, cycle_for, stage_to_dict
from .climate import (TemperatureData, date_when_actual_reaches, load_temperatures, normal_days_to_accumulate,
                      normal_gdd_between, thermal_progress)
from .config import settings
from .models import CropProfile, Plant, StageObservation, Zone

AI_MAX_AGE_DAYS = 7          # an AI photo older than this is not used for the current stage
AI_MIN_CONFIDENCE = 0.60     # below: the photo is shown but not used as agreement
TIMING_AGREEMENT_DAYS = 3    # calendar and degree-day estimates "agree" within this many days


def farm_today() -> date:
    return datetime.now(ZoneInfo(settings.timezone)).date()


def crop_and_planting(session: Session, zone: Zone, plant: Plant | None) -> tuple[CropProfile | None, date | None]:
    crop_id = (plant.crop_profile_id if plant and plant.crop_profile_id else None) or zone.crop_profile_id
    profile = session.get(CropProfile, crop_id) if crop_id else None
    planting = (plant.planting_date if plant and plant.planting_date else None) or zone.planting_date
    return profile, planting


def cycle_of(profile: CropProfile) -> CropCycle:
    return cycle_for(profile.key, profile.l_ini, profile.l_dev, profile.l_mid, profile.l_late)


def calendar_position(cycle: CropCycle, day: int) -> dict[str, Any]:
    d = max(day, 0)
    start = 0
    for i, s in enumerate(cycle.stages):
        if d < start + s.days:
            nxt = cycle.stages[i + 1] if i + 1 < len(cycle.stages) else None
            return {"index": i, "stage": s, "day_in_stage": d - start, "days_to_next": start + s.days - d,
                    "next": nxt}
        start += s.days
    last = cycle.stages[-1]
    return {"index": len(cycle.stages) - 1, "stage": last, "day_in_stage": d - (start - last.days),
            "days_to_next": None, "next": None, "after_cycle": True}


def _observations(session: Session, zone: Zone, plant: Plant | None):
    q = select(StageObservation).where(StageObservation.zone_id == zone.id)
    rows = session.exec(q.order_by(StageObservation.observed_on.desc(), StageObservation.ts.desc())).all()
    if plant is None:
        return [r for r in rows if r.plant_id is None]
    own = [r for r in rows if r.plant_id == plant.id]
    zone_level = [r for r in rows if r.plant_id is None]
    return own + [r for r in zone_level if r not in own]   # plant evidence first, zone as fallback


def _timing(session: Session, cycle: CropCycle, planting: date, anchor: StageObservation | None, today: date,
            temps: TemperatureData | None) -> dict[str, Any]:
    """Where the crop is in its cycle, by calendar and by degree-days."""
    ref_start = anchor.observed_on if anchor else planting
    start_cycle_day = cycle.start_day(anchor.stage_key) if anchor else 0
    calendar_day = start_cycle_day + (today - ref_start).days
    norm_start = ref_start
    if cycle.reference_planting:
        m, d = cycle.reference_planting
        norm_start = date(planting.year, m, d) + timedelta(days=start_cycle_day)
    out: dict[str, Any] = {"ref_start": ref_start, "norm_start": norm_start, "start_cycle_day": start_cycle_day,
                           "calendar_day": calendar_day, "thermal_day": None, "thermal": None, "temps": None}
    if cycle.thermal is None:
        out["thermal"] = {"available": False,
                          "reason": "No documented degree-day thresholds for this crop yet: calendar only."}
        return out
    if temps is None:
        temps = load_temperatures(session, ref_start, today)
    out["temps"] = temps
    tp = thermal_progress(temps, cycle.thermal, ref_start, today, norm_start)
    out["thermal"] = tp
    if tp.get("available"):
        out["thermal_day"] = start_cycle_day + tp["equivalent_days"]
    return out


def _stage_dates(cycle: CropCycle, tm: dict[str, Any], today: date, used_thermal: bool) -> list[dict[str, Any]]:
    """Estimated start date of every stage (past: from the actual season; future: normal temperatures)."""
    ref_start, norm_start, scd = tm["ref_start"], tm["norm_start"], tm["start_cycle_day"]
    position = tm["thermal_day"] if used_thermal else tm["calendar_day"]
    temps, params = tm["temps"], cycle.thermal
    out = []
    for i, s in enumerate(cycle.stages):
        k = cycle.start_day(s.key)
        if k <= position:                                      # already started
            if k < scd:
                d, basis = None, "before the farmer's confirmation"
            elif k == scd and scd > 0:
                d, basis = ref_start, "farmer confirmation"
            elif used_thermal:
                target = normal_gdd_between(temps, params, norm_start, k - scd)
                d = date_when_actual_reaches(temps, params, ref_start, today, target)
                basis = "degree-days of this season"
            else:
                d, basis = ref_start + timedelta(days=k - scd), "calendar"
        else:                                                  # still to come
            if used_thermal:
                remaining = (normal_gdd_between(temps, params, norm_start, k - scd)
                             - normal_gdd_between(temps, params, norm_start, position - scd))
                days = normal_days_to_accumulate(temps, params, today, remaining)
                d = today + timedelta(days=round(days)) if days is not None else None
                basis = "degree-days, normal temperatures assumed from today"
            else:
                d, basis = today + timedelta(days=round(k - position)), "calendar"
        out.append({"key": s.key, "name_en": s.name_en, "name_fr": s.name_fr, "visible": s.visible,
                    "start_cycle_day": k, "estimated_start": d.isoformat() if d else None, "basis": basis})
    for i, row in enumerate(out):
        nxt = out[i + 1]["estimated_start"] if i + 1 < len(out) else None
        row["estimated_end"] = nxt
    return out


def assess(session: Session, zone: Zone, plant: Plant | None = None, today: date | None = None,
           temps: TemperatureData | None = None) -> dict[str, Any]:
    today = today or farm_today()
    profile, planting = crop_and_planting(session, zone, plant)
    if profile is None or planting is None:
        return {"available": False, "reason": "No crop or planting date for this zone/plant."}
    cycle = cycle_of(profile)
    dap = (today - planting).days
    obs = _observations(session, zone, plant)
    obs = [o for o in obs if o.crop_key == profile.key]

    basis: list[dict[str, Any]] = [{"kind": "calendar", "detail": f"{dap} days after planting ({planting:%d/%m/%Y})"}]
    anchor = next((o for o in obs if o.status == "confirmed" and o.stage_key), None)
    if anchor:
        basis.append({"kind": "farmer_confirmation", "stage": anchor.stage_key,
                      "detail": f"confirmed on {anchor.observed_on:%d/%m/%Y}" + (" (this plant)" if anchor.plant_id else "")})

    tm = _timing(session, cycle, planting, anchor, today, temps)
    used_thermal = tm["thermal_day"] is not None
    position = tm["thermal_day"] if used_thermal else float(tm["calendar_day"])
    effective_day = int(round(position))
    th = tm["thermal"] or {}
    if used_thermal:
        diff = tm["thermal_day"] - tm["calendar_day"]
        trend = "ahead of" if diff > 0.5 else "behind" if diff < -0.5 else "in line with"
        basis.append({"kind": "degree_days",
                      "detail": f"{th['actual_gdd']} °C·d since {tm['ref_start']:%d/%m/%Y} vs {th['reference_gdd']} in the "
                                f"reference season (from {tm['norm_start']:%d/%m}): {abs(diff):.1f} days {trend} the calendar",
                      "coverage": th["coverage"], "days_by_source": th["days_by_source"]})

    pos = calendar_position(cycle, effective_day)
    stage = pos["stage"]
    needs_confirmation, reasons, reasons_fr = False, [], []
    agreement = "calendar_only" if not anchor else "farmer_anchored"

    since = anchor.observed_on if anchor else date.min
    ai = next((o for o in obs if o.source == "ai" and o.status in ("estimated", "needs_confirmation")
               and o.observed_on >= since
               and (today - o.observed_on).days <= AI_MAX_AGE_DAYS), None)
    if ai:
        cands = list(ai.candidate_stages)
        basis.append({"kind": "ai_photo", "visual_class": ai.visual_class, "candidates": cands,
                      "confidence": ai.confidence, "detail": f"photo of {ai.observed_on:%d/%m/%Y}"})
        if (ai.confidence or 0) < AI_MIN_CONFIDENCE:
            reasons.append("The last photo is not clear enough to check the stage.")
            reasons_fr.append("La dernière photo n'est pas assez nette pour vérifier le stade.")
        elif stage.key in cands:
            agreement = "ai_agrees" if len(cands) == 1 else "ai_compatible"
        else:
            idx = [cycle.index(c) for c in cands]
            anchor_idx = cycle.index(anchor.stage_key) if anchor else -1
            if idx and max(idx) < anchor_idx:
                reasons.append("The photo suggests an earlier stage than the one you confirmed: please check.")
                reasons_fr.append("La photo suggère un stade antérieur à celui que vous avez confirmé : à vérifier.")
            else:
                names = ", ".join(cycle.get(c).name_en for c in cands) or ai.visual_class
                reasons.append(f"The photo suggests {names}, the calendar says {stage.name_en}: please confirm.")
                names_fr = ", ".join(cycle.get(c).name_fr for c in cands) or ai.visual_class
                reasons_fr.append(f"La photo suggère : {names_fr} ; le calendrier indique : {stage.name_fr}. À confirmer.")
            agreement = "disagreement"
            needs_confirmation = True

    field_check_due = False
    confirmed_this_stage = anchor is not None and anchor.stage_key == stage.key
    if not stage.visible and not confirmed_this_stage:
        field_check_due = True
        needs_confirmation = True
        reasons.append(f"{stage.name_en} happens underground and cannot be seen on a photo. {stage.field_check}")
        reasons_fr.append(f"{stage.name_fr} : se passe sous terre, invisible sur une photo. Déterrer un plant pour vérifier.")

    if pos.get("after_cycle"):
        reasons.append("Past the end of the reference cycle: confirm harvest or update the planting date.")
        reasons_fr.append("Fin du cycle de référence dépassée : confirmer la récolte ou corriger la date de plantation.")
        needs_confirmation = True

    previous = None
    last_recorded = next((o for o in obs if o.stage_key and o.stage_key != stage.key and o.status != "rejected"), None)
    if last_recorded:
        previous = {"stage": last_recorded.stage_key, "on": last_recorded.observed_on.isoformat(),
                    "source": last_recorded.source}

    # ---- range of plausible positions (calendar vs degree-days) and possible stages
    days_range = sorted({tm["calendar_day"], round(tm["thermal_day"], 1)} if used_thermal else {tm["calendar_day"]})
    possible = []
    for d in (days_range[0], days_range[-1]):
        k = calendar_position(cycle, int(round(d)))["stage"].key
        if k not in possible:
            possible.append(k)

    # ---- dates
    timeline = _stage_dates(cycle, tm, today, used_thermal)
    cur = timeline[pos["index"]]
    nxt_row = timeline[pos["index"] + 1] if pos["next"] else None
    days_to_next = None
    if nxt_row and nxt_row["estimated_start"]:
        days_to_next = (date.fromisoformat(nxt_row["estimated_start"]) - today).days

    # ---- certainty (no invented percentage: a level with its reasons)
    cert_reasons, cert_fr, score = [], [], 0
    if confirmed_this_stage and anchor and (today - anchor.observed_on).days <= stage.days:
        level = "confirmed"
        cert_reasons.append(f"You confirmed {stage.name_en} on {anchor.observed_on:%d/%m/%Y}.")
        cert_fr.append(f"Vous avez confirmé « {stage.name_fr} » le {anchor.observed_on:%d/%m/%Y}.")
    else:
        if used_thermal:
            score += 1
            cert_reasons.append("Corrected with the degree-days of this season.")
            cert_fr.append("Corrigé par les degrés-jours de la saison.")
            if abs(tm["thermal_day"] - tm["calendar_day"]) <= TIMING_AGREEMENT_DAYS:
                cert_reasons.append("Calendar and degree-days agree.")
                cert_fr.append("Le calendrier et les degrés-jours concordent.")
            else:
                score -= 1
                cert_fr.append(f"Le calendrier et les degrés-jours diffèrent de plus de {TIMING_AGREEMENT_DAYS} jours.")
                cert_reasons.append("Calendar and degree-days differ by more than "
                                    f"{TIMING_AGREEMENT_DAYS} days.")
        else:
            cert_reasons.append("Timing from the calendar only: " + th.get("reason", "no temperature correction."))
            cert_fr.append("Estimation par le calendrier seul (pas de correction par la température : "
                           + ("données climatiques indisponibles" if "normal" in th.get("reason", "") else
                              "températures réelles insuffisantes" if "real temperature" in th.get("reason", "") else
                              "pas de seuils documentés pour cette culture") + ").")
        if agreement in ("ai_agrees", "ai_compatible"):
            score += 1
            cert_reasons.append("The last photo is compatible with this stage.")
            cert_fr.append("La dernière photo est compatible avec ce stade.")
        if anchor:
            score += 1
            cert_reasons.append(f"Counted from your confirmation of {anchor.observed_on:%d/%m/%Y}.")
            cert_fr.append(f"Compté à partir de votre confirmation du {anchor.observed_on:%d/%m/%Y}.")
        if len(possible) > 1:
            score -= 1
            cert_reasons.append("Close to a stage change: " + " or ".join(cycle.get(k).name_en for k in possible) + ".")
            cert_fr.append("Proche d'un changement de stade : " + " ou ".join(cycle.get(k).name_fr for k in possible) + ".")
        level = "high" if score >= 2 else "medium" if score == 1 else "low"
        if needs_confirmation:
            level = "low"
            cert_reasons.append("A confirmation is needed (see reasons).")
            cert_fr.append("Une confirmation est nécessaire (voir ci-dessus).")

    confidence_label = {"farmer_anchored": "confirmed by the farmer", "ai_agrees": "calendar and photo agree",
                        "ai_compatible": "photo compatible with the calendar", "calendar_only": "calendar estimate",
                        "disagreement": "calendar and photo disagree"}[agreement]
    thermal_summary = {k: v for k, v in th.items() if k != "temps"} if th else None
    return {
        "available": True,
        "zone_id": zone.id, "plant_id": plant.id if plant else None,
        "crop_key": profile.key, "crop_name": profile.name, "cycle_generic": cycle.generic,
        "planting_date": planting.isoformat(), "today": today.isoformat(),
        "stage": stage_to_dict(stage), "stage_index": pos["index"], "stage_count": len(cycle.stages),
        "stage_start_date": cur["estimated_start"], "stage_start_basis": cur["basis"],
        "days_after_planting": dap, "effective_day": effective_day,
        "day_in_stage": pos["day_in_stage"], "days_to_next_stage": days_to_next,
        "next_stage": stage_to_dict(pos["next"]) if pos["next"] else None,
        "next_stage_date": nxt_row["estimated_start"] if nxt_row else None,
        "next_stage_basis": nxt_row["basis"] if nxt_row else None,
        "cycle_length_days": cycle.length,
        "cycle_progress_pct": round(min(max(position / cycle.length, 0.0), 1.0) * 100, 1),
        "stage_progress_pct": round(min(pos["day_in_stage"] / stage.days, 1.0) * 100, 1),
        "timing": {"used": "degree_days" if used_thermal else "calendar",
                   "calendar_day": tm["calendar_day"],
                   "thermal_day": round(tm["thermal_day"], 1) if used_thermal else None,
                   "range_days": days_range, "possible_stages": possible,
                   "thermal": thermal_summary},
        "timeline": timeline,
        "certainty": {"level": level, "reasons": cert_reasons, "reasons_fr": cert_fr},
        "agreement": agreement, "confidence_label": confidence_label,
        "needs_confirmation": needs_confirmation, "field_check_due": field_check_due,
        "reasons": reasons, "reasons_fr": reasons_fr, "basis": basis, "previous_stage": previous,
        "kind": "confirmed" if agreement == "farmer_anchored" and not needs_confirmation else "estimated",
    }


def record_transition(session: Session, zone: Zone, assessment: dict, plant: Plant | None = None) -> StageObservation | None:
    """Store a 'system' row when the estimated stage changes, so the history shows each transition."""
    if not assessment.get("available"):
        return None
    last = session.exec(select(StageObservation).where(StageObservation.zone_id == zone.id,
                                                       StageObservation.plant_id == (plant.id if plant else None),
                                                       StageObservation.stage_key.is_not(None),
                                                       StageObservation.status != "rejected")
                        .order_by(StageObservation.observed_on.desc(), StageObservation.ts.desc())).first()
    key = assessment["stage"]["key"]
    if last and last.stage_key == key:
        return None
    row = StageObservation(zone_id=zone.id, plant_id=plant.id if plant else None, crop_key=assessment["crop_key"],
                           source="system", stage_key=key, status="estimated",
                           note=f"Entered {assessment['stage']['name_en']} ({assessment['confidence_label']})")
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def history(session: Session, zone: Zone, plant: Plant | None = None) -> list[dict[str, Any]]:
    rows = _observations(session, zone, plant)
    return [{"id": o.id, "on": o.observed_on.isoformat(), "ts": o.ts.isoformat(), "source": o.source,
             "plant_id": o.plant_id, "stage_key": o.stage_key, "visual_class": o.visual_class,
             "candidates": o.candidate_stages, "confidence": o.confidence, "status": o.status,
             "visual_cues": o.visual_cues, "image_ref": o.image_ref, "note": o.note} for o in rows]


def record_ai_stage(session: Session, zone: Zone, crop_key: str, visual_class: str, confidence: float | None,
                    plant: Plant | None = None, visual_cues: list[str] | None = None, image_ref: str | None = None,
                    vision_observation_id: int | None = None, today: date | None = None) -> StageObservation | None:
    profile, _ = crop_and_planting(session, zone, plant)
    if profile is None or profile.key != crop_key:
        return None                                    # photo of another crop: never mixed into this cycle
    cycle = cycle_of(profile)
    cands = list(cycle.visual_classes.get(visual_class, ()))
    if not cands:
        return None
    row = StageObservation(zone_id=zone.id, plant_id=plant.id if plant else None, crop_key=crop_key, source="ai",
                           visual_class=visual_class, candidate_stages=cands,
                           stage_key=cands[0] if len(cands) == 1 else None, confidence=confidence,
                           visual_cues=visual_cues or [], status="estimated", image_ref=image_ref,
                           vision_observation_id=vision_observation_id,
                           observed_on=today or farm_today())
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def confirm_stage(session: Session, zone: Zone, crop_key: str, stage_key: str, plant: Plant | None = None,
                  observed_on: date | None = None, note: str | None = None,
                  from_observation: StageObservation | None = None) -> StageObservation:
    profile, _ = crop_and_planting(session, zone, plant)
    if profile is None or profile.key != crop_key:
        raise ValueError("This zone/plant is not registered with that crop.")
    cycle_of(profile).index(stage_key)                 # raises KeyError for a stage of another crop
    # the farmer's word settles the open photo proposals made up to now for this zone/plant
    open_ai = session.exec(select(StageObservation).where(
        StageObservation.zone_id == zone.id, StageObservation.source == "ai",
        StageObservation.status.in_(("estimated", "needs_confirmation")))).all()
    for o in open_ai:
        if (plant is None or o.plant_id in (None, plant.id)) and (from_observation is None or o.id != from_observation.id):
            o.status = "superseded"
            o.note = (o.note + " | " if o.note else "") + "superseded by the farmer's confirmation"
            session.add(o)
    if from_observation is not None:
        # the farmer validated (or corrected) the photo: it becomes the farmer's confirmation
        from_observation.status, from_observation.stage_key = "confirmed", stage_key
        from_observation.source = "farmer"
        from_observation.note = note or from_observation.note
        session.add(from_observation)
        session.commit()
        session.refresh(from_observation)
        return from_observation
    row = StageObservation(zone_id=zone.id, plant_id=plant.id if plant else None, crop_key=crop_key,
                           source="farmer", stage_key=stage_key, status="confirmed", note=note,
                           observed_on=observed_on or farm_today())
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def find_or_create_plant(session: Session, zone: Zone, label: str) -> Plant:
    p = session.exec(select(Plant).where(Plant.zone_id == zone.id, Plant.label == label)).first()
    if p:
        return p
    p = Plant(zone_id=zone.id, label=label)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


__all__ = ["assess", "history", "record_ai_stage", "record_transition", "confirm_stage", "find_or_create_plant",
           "calendar_position", "cycle_of", "timedelta"]
