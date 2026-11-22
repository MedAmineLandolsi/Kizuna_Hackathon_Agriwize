"""Aggregated views for the two dashboard pages (irrigation flow, camera procedure).

Read-only: every value comes from the existing tables and engines (decision, growth, irrigation plan).
Each value keeps its provenance (measured / calculated / forecast / ai / missing) so the pages can show it.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from . import growth, services
from .config import settings
from .db import get_session
from .irrigation_plan import build_plan, plan_summary
from pydantic import BaseModel

from .models import (Anomaly, CropProfile, DashboardLayout, Decision, IrrigationCommand, SensorReading, StageObservation,
                     VisionObservation, WeatherDay, Zone, utcnow)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

CONTROLLER_OFFLINE_MIN = 5       # no message from the ESP32 for this long -> offline
ACTIVE = ("pending", "sent", "running", "abort_requested")

# the 9 sensors of the AGRIWISE spec; those without a field in the database are "not installed"
SENSORS = [
    # key, name_fr, unit, where, field, low, high
    ("soil_moisture", "Humidité du sol", "m³/m³", "zone", "soil_theta", None, None),
    ("soil_temp", "Température du sol", "°C", "zone", "soil_temp_c", 10, 35),
    ("air_temp", "Température de l'air", "°C", "controller", "air_temp_c", 8, 38),
    ("air_rh", "Humidité de l'air", "%", "controller", "air_rh_pct", 25, 90),
    ("light", "Luminosité", "lux", "controller", None, None, None),
    ("rain", "Pluie (capteur)", "", "controller", "rain_sensor_wet", None, None),
    ("flow", "Débit", "L/min", "controller", "flow_lpm", None, None),
    ("ph", "pH du sol", "", "zone", None, 5.5, 7.5),
    ("ec", "Conductivité (EC)", "dS/m", "zone", "soil_ec_ds_m", None, 2.5),
]


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() + "Z" if dt else None


def _age_s(dt: Optional[datetime], now: datetime) -> Optional[float]:
    return round((now - dt).total_seconds(), 0) if dt else None


def _v(decision: Optional[Decision], key: str) -> Any:
    if decision is None:
        return None
    val = (decision.values or {}).get(key)
    return val.get("value") if isinstance(val, dict) else val


ALERT_FR = {
    "identification": "{z} : la caméra n'est pas sûre de la culture. Confirmer ou rejeter la photo (page Caméra) ; "
                      "l'irrigation continue en attendant.",
    "insect_trap": "{z} : nombreux insectes sur le piège jaune. Inspecter les plantes.",
    "leak": "Débit mesuré alors que toutes les vannes sont fermées : fuite ou vanne bloquée.",
    "low_delivery": "{z} : volume apporté trop faible (tuyau bouché, vanne manuelle fermée, pompe ou pression).",
    "over_delivery": "{z} : volume apporté trop élevé (vanne restée ouverte ou débitmètre défaillant).",
    "moisture_response": "{z} : l'humidité du sol a peu augmenté après l'irrigation (fuite après le débitmètre, "
                         "goutteur bouché ou capteur mal placé).",
    "motion": "{z} : mouvement détecté dans la parcelle.",
    "rain_forecast": "Pluie prévue : l'irrigation pourra être reportée.",
    "sensor": "{z} : mesure de capteur invalide ; irrigation automatique suspendue pour cette zone.",
    "animal": "{z} : animal vu dans la parcelle ({x}).",
    "pest": "{z} : ravageur possible ({x}) signalé par le détecteur entraîné. Vérifier sur les plantes.",
}


def _anomaly(a: Anomaly, zone_names: Optional[dict[int, str]] = None) -> dict[str, Any]:
    kind, _, extra = a.kind.partition(":")
    tpl = ALERT_FR.get(kind)
    zname = (zone_names or {}).get(a.zone_id, "Ferme") if a.zone_id else "Ferme"
    return {"id": a.id, "zone_id": a.zone_id, "kind": a.kind, "severity": a.severity, "message": a.message,
            "message_fr": tpl.format(z=zname, x=extra.replace("_", " ")) if tpl else a.message,
            "blocks": a.blocks_irrigation, "ts": _iso(a.ts)}


def _command(c: Optional[IrrigationCommand]) -> Optional[dict[str, Any]]:
    if c is None:
        return None
    return {"id": c.id, "origin": c.origin, "status": c.status, "target_l": c.target_volume_l,
            "delivered_l": c.delivered_l, "stop_reason": c.stop_reason, "created_at": _iso(c.created_at),
            "started_at": _iso(c.started_at), "finished_at": _iso(c.finished_at),
            "verification": c.verification or {}}


def _sensor_block(zone: Zone, zr: Optional[SensorReading], cr: Optional[SensorReading], now: datetime,
                  fc: float, wp: float) -> list[dict[str, Any]]:
    out = []
    for key, name, unit, where, fld, low, high in SENSORS:
        r = zr if where == "zone" else cr
        item: dict[str, Any] = {"key": key, "name": name, "unit": unit, "where": where}
        if fld is None:
            item.update(status="not_installed", value=None, kind="missing",
                        note="Pas de capteur branché sur l'ESP32 pour cette mesure.")
            out.append(item)
            continue
        value = getattr(r, fld) if r is not None else None
        if r is None or value is None:
            item.update(status="no_data", value=None, kind="missing", note="Aucune mesure reçue.")
            out.append(item)
            continue
        age = _age_s(r.ts, now)
        item.update(value=value, kind="measured", ts=_iso(r.ts), age_s=age)
        if not r.valid:
            item.update(status="anomaly", note="; ".join(r.issues))
        elif age is not None and age > settings.reading_max_age_min * 60:
            item.update(status="anomaly", note=f"Mesure trop ancienne ({int(age // 60)} min).")
        elif key == "soil_moisture":
            item.update(status="low" if value <= wp + 0.5 * (fc - wp) else "high" if value > fc else "normal",
                        range=[round(wp, 3), round(fc, 3)])
        elif key == "rain":
            item.update(status="normal", note="Pluie détectée" if value else "Sec")
        elif key == "flow":
            item.update(status="normal")
        else:
            status = "normal"
            if low is not None and value < low:
                status = "low"
            if high is not None and value > high:
                status = "high"
            item.update(status=status, range=[low, high])
        out.append(item)
    return out


def _flow_steps(zone: Zone, ctrl_online: bool, zr: Optional[SensorReading], decision: Optional[Decision],
                plan: dict[str, Any], active: Optional[IrrigationCommand], last_done: Optional[IrrigationCommand],
                cr: Optional[SensorReading], anomalies: list[Anomaly], emergency: bool, now: datetime,
                weather_today: Optional[WeatherDay]) -> list[dict[str, Any]]:
    """The 6 stations of the irrigation flow, each with a state ok | warn | error | idle | active."""
    steps = []
    # 1. sensors
    if not ctrl_online:
        steps.append({"key": "sensors", "title": "Capteurs ESP32", "state": "error",
                      "value": "Hors ligne", "detail": "Aucun message de l'ESP32 depuis plus de "
                                                       f"{CONTROLLER_OFFLINE_MIN} min."})
    elif zr is None or zr.soil_theta is None:
        steps.append({"key": "sensors", "title": "Capteurs ESP32", "state": "warn", "value": "Pas de mesure sol",
                      "detail": "L'ESP32 répond mais n'envoie pas l'humidité de cette zone."})
    else:
        ok = zr.valid
        steps.append({"key": "sensors", "title": "Capteurs ESP32", "state": "ok" if ok else "error",
                      "value": f"{zr.soil_theta * 100:.1f} % vol.", "kind": "measured",
                      "detail": ("Humidité du sol mesurée il y a " + _ago(zr.ts, now)) if ok
                      else "Mesure invalide : " + "; ".join(zr.issues)})
    # 2. FAO-56 calculation
    et0, etc, dr, raw = (_v(decision, k) for k in ("et0", "etc", "depletion", "raw"))
    if decision is None or et0 is None:
        steps.append({"key": "fao", "title": "Calcul FAO-56", "state": "warn", "value": "Pas encore calculé",
                      "detail": "Il manque l'ET0 (prévision météo ou température de l'air)."})
    else:
        src = ((decision.values or {}).get("et0") or {}).get("source", "")
        detail = f"ET0 {et0:.2f} mm · ETc {etc:.2f} mm/j" if etc is not None else f"ET0 {et0:.2f} mm"
        if dr is not None and raw is not None:
            detail += f" · réserve utilisée {dr:.1f} / {raw:.1f} mm"
        steps.append({"key": "fao", "title": "Calcul FAO-56", "state": "ok", "kind": "calculated",
                      "value": f"ETc {etc:.2f} mm/j" if etc is not None else f"ET0 {et0:.2f} mm/j",
                      "detail": detail, "source": src})
    # 3. decision
    action_fr = {"irrigate_now": "Irriguer maintenant", "schedule": "Irrigation programmée",
                 "no_irrigation": "Pas besoin d'irriguer", "postpone_rain": "Reportée (pluie)",
                 "blocked": "Bloquée", "fixed_schedule": "Calendrier fixe"}
    if decision is None:
        steps.append({"key": "decision", "title": "Décision", "state": "idle", "value": "Aucune décision",
                      "detail": "Le moteur n'a pas encore tourné pour cette zone."})
    else:
        st = {"blocked": "error", "postpone_rain": "warn"}.get(decision.action, "ok")
        freq = (plan.get("frequency") or {}).get("text_fr")
        steps.append({"key": "decision", "title": "Décision", "state": st, "kind": "calculated",
                      "value": action_fr.get(decision.action, decision.action),
                      "detail": decision_fr(decision, settings.timezone)
                      + (f" Fréquence estimée : {freq}." if freq else ""),
                      "ts": _iso(decision.ts)})
    # 4. valve
    if emergency:
        steps.append({"key": "valve", "title": "Électrovanne", "state": "error", "value": "Arrêt d'urgence",
                      "detail": "Toutes les vannes sont forcées fermées."})
    elif active is not None and active.status in ("sent", "running"):
        steps.append({"key": "valve", "title": "Électrovanne", "state": "active", "value": "Ouverte",
                      "detail": f"Commande n°{active.id} ({active.origin}) : objectif {active.target_volume_l:.0f} L."})
    elif active is not None:
        steps.append({"key": "valve", "title": "Électrovanne", "state": "warn", "value": "En attente",
                      "detail": f"Commande n°{active.id} en attente de l'ESP32 ({active.status})."})
    else:
        steps.append({"key": "valve", "title": "Électrovanne", "state": "idle", "value": "Fermée",
                      "detail": f"Canal {zone.valve_channel}."})
    # 5. flow meter
    flow = cr.flow_lpm if cr is not None else None
    open_valves = (cr.valves_open or []) if cr is not None else []
    if active is not None and active.status in ("sent", "running"):
        steps.append({"key": "flow", "title": "Débitmètre", "state": "active" if flow else "warn", "kind": "measured",
                      "value": f"{flow:.1f} L/min" if flow is not None else "—",
                      "detail": "Débit mesuré pendant l'irrigation." if flow else
                      "Vanne ouverte mais aucun débit mesuré : vérifier pompe, pression, tuyau."})
    elif last_done is not None:
        ver = last_done.verification or {}
        ok = ver.get("volume_ok")
        steps.append({"key": "flow", "title": "Débitmètre", "kind": "measured",
                      "state": "ok" if ok else "error" if ok is False else "idle",
                      "value": f"{(last_done.delivered_l or 0):.1f} / {last_done.target_volume_l:.0f} L",
                      "detail": f"Dernière irrigation : {_ago(last_done.finished_at, now)}"
                                + (" — volume conforme." if ok else " — écart de volume." if ok is False else ".")})
    else:
        leak = bool(flow) and not open_valves and flow > settings.leak_flow_threshold_lpm
        steps.append({"key": "flow", "title": "Débitmètre", "state": "error" if leak else "idle",
                      "value": f"{flow:.1f} L/min" if flow is not None else "—",
                      "detail": "Débit avec toutes les vannes fermées : fuite ?" if leak else "Aucune irrigation."})
    # 6. verification
    ver = (last_done.verification or {}) if last_done is not None else {}
    zone_alerts = [a for a in anomalies if a.zone_id in (zone.id, None)]
    if any(a.blocks_irrigation for a in zone_alerts):
        steps.append({"key": "check", "title": "Vérification", "state": "error", "value": "Alerte bloquante",
                      "detail": zone_alerts[0].message})
    elif "response_ratio" in ver:
        r = ver["response_ratio"]
        ok = r is not None and r >= settings.moisture_response_min_ratio
        steps.append({"key": "check", "title": "Vérification", "state": "ok" if ok else "warn", "kind": "measured",
                      "value": f"Sol +{ver['measured_gain'] * 100:.1f} pts",
                      "detail": f"Attendu +{ver['expected_gain'] * 100:.1f} pts "
                                f"({'réponse correcte' if ok else 'réponse faible : fuite ou goutteur bouché ?'})."})
    elif last_done is not None and last_done.theta_after is None:
        steps.append({"key": "check", "title": "Vérification", "state": "idle", "value": "En attente",
                      "detail": f"Contrôle de l'humidité {settings.moisture_check_delay_min:.0f} min après l'irrigation."})
    else:
        steps.append({"key": "check", "title": "Vérification", "state": "warn" if zone_alerts else "idle",
                      "value": f"{len(zone_alerts)} alerte(s)" if zone_alerts else "Rien à vérifier",
                      "detail": zone_alerts[0].message if zone_alerts else ""})
    return steps


BLOCK_FR = [
    ("Emergency stop", "Arrêt d'urgence actif."),
    ("manual mode", "Mode manuel : irrigation automatique désactivée."),
    ("Zone disabled", "Zone désactivée."),
    ("No crop profile", "Aucune culture enregistrée pour cette zone."),
    ("identification not confirmed", "Culture à confirmer par l'agriculteur."),
    ("Planting date missing", "Date de plantation manquante."),
    ("already pending or running", "Une irrigation est déjà en cours pour cette zone."),
    ("Water tank level", "Niveau du réservoir trop bas."),
    ("No soil moisture reading", "Aucune mesure d'humidité du sol."),
    ("Invalid soil moisture", "Mesure d'humidité du sol invalide."),
    ("older than", "Mesure d'humidité du sol trop ancienne."),
]


def decision_fr(decision: Decision, tz: str) -> str:
    """Short French explanation built from the decision's own values (the engine writes English)."""
    dr, raw, days = _v(decision, "depletion"), _v(decision, "raw"), _v(decision, "days_until_irrigation")
    vol = decision.target_volume_l
    a = decision.action
    if a == "blocked":
        for en, fr in BLOCK_FR:
            if decision.reasons and en in decision.reasons[0]:
                return fr
        return decision.reasons[0] if decision.reasons else "Bloquée."
    if a == "no_irrigation" and dr is not None and raw is not None:
        txt = f"Réserve utilisée {dr:.1f} mm sur un seuil de {raw:.1f} mm : pas de stress."
        return txt + (f" Prochaine irrigation dans ~{days:.1f} j." if days is not None else "")
    if a == "irrigate_now":
        return (f"Réserve utilisée {dr:.1f} mm > seuil {raw:.1f} mm : irriguer {vol:.0f} L maintenant."
                if dr is not None and raw is not None and vol else "Irrigation lancée.")
    if a == "schedule" and decision.scheduled_for:
        from zoneinfo import ZoneInfo
        local = decision.scheduled_for.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz))
        return (f"Seuil dépassé ({dr:.1f} > {raw:.1f} mm) : irrigation de {vol:.0f} L programmée à "
                f"{local:%H:%M} (fenêtre d'irrigation, moins d'évaporation).") if dr is not None and vol else \
            f"Irrigation programmée à {local:%H:%M}."
    if a == "postpone_rain":
        return "Pluie observée ou prévue suffisante : irrigation reportée."
    if a == "fixed_schedule":
        return "Zone témoin : irriguée à heure fixe pour la comparaison."
    return decision.reasons[0] if decision.reasons else ""


def _ago(dt: Optional[datetime], now: datetime) -> str:
    if dt is None:
        return "—"
    s = (now - dt).total_seconds()
    if s < 90:
        return f"{int(s)} s"
    if s < 5400:
        return f"{int(s // 60)} min"
    if s < 172800:
        return f"{int(s // 3600)} h"
    return f"{int(s // 86400)} j"


ACTION_FR = {"irrigate_now": "Irriguer maintenant", "schedule": "Irrigation programmée",
             "no_irrigation": "Pas besoin d'irriguer", "postpone_rain": "Reportée (pluie)",
             "blocked": "Bloquée", "fixed_schedule": "Calendrier fixe"}


def _vision_summary(session: Session, zone: Zone) -> Optional[dict[str, Any]]:
    o = session.exec(select(VisionObservation).where(VisionObservation.zone_id == zone.id)
                     .order_by(VisionObservation.ts.desc())).first()
    if o is None:
        return None
    p = o.payload or {}
    health = p.get("health") or {}
    pests = p.get("pests") or []
    so = session.exec(select(StageObservation).where(StageObservation.vision_observation_id == o.id)).first()
    return {"id": o.id, "ts": _iso(o.ts), "source": o.source, "image_ref": o.image_ref,
            "crop_key": o.crop_key, "crop_fr": CROP_FR.get(o.crop_key) if o.crop_key else None,
            "crop_confidence": o.crop_confidence,
            "stage_fr": VISUAL_FR.get(o.growth_stage) if o.growth_stage else None,
            "health_fr": (", ".join(x["label"].replace("_", " ") for x in pests) + " (détecteur entraîné)") if pests
            else health.get("message_fr"),
            "health_ok": not pests and health.get("hint") in (None, "healthy"),
            "trap_count": (p.get("trap") or {}).get("insect_count"),
            "stage_status": ("needs_confirmation" if so and so.status == "needs_confirmation" else so.status) if so else None}


def _procedure(zone: Zone, zr: Optional[SensorReading], vision: Optional[dict], decision: Optional[Decision],
               active: Optional[IrrigationCommand], last_done: Optional[IrrigationCommand], ctrl_online: bool,
               emergency: bool, plan: dict[str, Any], now: datetime) -> dict[str, Any]:
    """The main cycle: measure -> observe -> calculate -> decide -> irrigate -> verify."""
    steps = []
    # 1 measure
    if zr is None or zr.soil_theta is None:
        steps.append({"key": "measure", "title": "Mesurer", "state": "error" if not ctrl_online else "warn",
                      "value": "Aucune mesure" if ctrl_online else "ESP32 hors ligne", "ts": None})
    else:
        steps.append({"key": "measure", "title": "Mesurer", "ts": _iso(zr.ts),
                      "state": "ok" if zr.valid and ctrl_online else "error",
                      "value": f"sol {zr.soil_theta * 100:.1f} %" if zr.valid else "mesure invalide"})
    # 2 observe
    if vision is None:
        steps.append({"key": "observe", "title": "Observer", "state": "idle", "value": "aucune image", "ts": None})
    else:
        val = " · ".join(x for x in (vision["crop_fr"], vision["stage_fr"]) if x) or "image reçue"
        steps.append({"key": "observe", "title": "Observer", "ts": vision["ts"], "value": val,
                      "state": "warn" if vision["stage_status"] == "needs_confirmation" or not vision["health_ok"] else "ok"})
    # 3 calculate
    etc, dr, raw = _v(decision, "etc"), _v(decision, "depletion"), _v(decision, "raw")
    if decision is None or etc is None:
        steps.append({"key": "calculate", "title": "Calculer", "state": "warn", "value": "ET0 manquante",
                      "ts": _iso(decision.ts) if decision else None})
    else:
        steps.append({"key": "calculate", "title": "Calculer", "state": "ok", "ts": _iso(decision.ts),
                      "value": f"ETc {etc:.1f} mm/j" + (f" · {dr:.0f}/{raw:.0f} mm" if dr is not None and raw is not None else "")})
    # 4 decide
    if decision is None:
        steps.append({"key": "decide", "title": "Décider", "state": "idle", "value": "—", "ts": None})
    else:
        steps.append({"key": "decide", "title": "Décider", "ts": _iso(decision.ts),
                      "state": {"blocked": "error", "postpone_rain": "warn"}.get(decision.action, "ok"),
                      "value": ACTION_FR.get(decision.action, decision.action)})
    # 5 irrigate
    if emergency:
        steps.append({"key": "irrigate", "title": "Irriguer", "state": "error", "value": "arrêt d'urgence", "ts": None})
    elif active is not None and active.status in ("sent", "running"):
        steps.append({"key": "irrigate", "title": "Irriguer", "state": "active", "ts": _iso(active.started_at or active.created_at),
                      "value": f"en cours · {active.target_volume_l:.0f} L"})
    elif active is not None:
        steps.append({"key": "irrigate", "title": "Irriguer", "state": "warn", "ts": _iso(active.scheduled_for),
                      "value": f"programmée · {active.target_volume_l:.0f} L"})
    elif last_done is not None:
        steps.append({"key": "irrigate", "title": "Irriguer", "state": "ok", "ts": _iso(last_done.finished_at),
                      "value": f"{(last_done.delivered_l or 0):.0f} L apportés"})
    else:
        nxt = plan.get("next_irrigation")
        steps.append({"key": "irrigate", "title": "Irriguer", "state": "idle", "ts": None,
                      "value": f"prochaine {nxt['when_fr']}" if nxt else "aucune"})
    # 6 verify
    ver = (last_done.verification or {}) if last_done else {}
    if last_done is None:
        steps.append({"key": "verify", "title": "Vérifier", "state": "idle", "value": "—", "ts": None})
    elif "response_ratio" in ver:
        ok = ver.get("volume_ok", True) and (ver["response_ratio"] or 0) >= settings.moisture_response_min_ratio
        steps.append({"key": "verify", "title": "Vérifier", "state": "ok" if ok else "warn", "ts": _iso(last_done.finished_at),
                      "value": f"volume {'OK' if ver.get('volume_ok') else 'écart'} · sol +{ver['measured_gain'] * 100:.1f} pts"})
    else:
        steps.append({"key": "verify", "title": "Vérifier", "ts": _iso(last_done.finished_at),
                      "state": "ok" if ver.get("volume_ok") else "error" if ver.get("volume_ok") is False else "warn",
                      "value": ("volume OK" if ver.get("volume_ok") else "écart de volume") + " · sol en attente"})
    # where the system is now
    if active is not None and active.status in ("sent", "running"):
        current, note = "irrigate", "L'eau est en train d'être apportée."
    elif last_done is not None and last_done.theta_after is None and last_done.finished_at and \
            (now - last_done.finished_at) < timedelta(hours=6):
        current, note = "verify", f"Contrôle de l'humidité {settings.moisture_check_delay_min:.0f} min après l'irrigation."
    elif decision is not None and decision.action in ("schedule", "irrigate_now") or active is not None:
        current, note = "decide", "Irrigation décidée, en attente de la fenêtre ou de l'ESP32."
    else:
        current, note = "measure", "Surveillance : nouvelle mesure et nouveau calcul toutes les quelques minutes."
    return {"steps": steps, "current": current, "note": note}


@router.get("/overview")
def overview(session: Session = Depends(get_session)) -> dict[str, Any]:
    now = utcnow()
    state = services.system_state(session)
    ctrl_age = _age_s(state.controller_last_seen, now)
    ctrl_online = ctrl_age is not None and ctrl_age <= CONTROLLER_OFFLINE_MIN * 60
    cr = services.latest_controller_reading(session)
    today = services.local_today(now, settings)
    wt = session.exec(select(WeatherDay).where(WeatherDay.day == today)).first()
    anomalies = services.open_anomalies(session)
    zone_names = {z.id: z.name for z in session.exec(select(Zone)).all()}
    zones_out = []
    for zone in session.exec(select(Zone).order_by(Zone.id)).all():
        profile = session.get(CropProfile, zone.crop_profile_id) if zone.crop_profile_id else None
        zr = services.latest_zone_reading(session, zone.id)
        decision = session.exec(select(Decision).where(Decision.zone_id == zone.id)
                                .order_by(Decision.ts.desc())).first()
        active = session.exec(select(IrrigationCommand).where(IrrigationCommand.zone_id == zone.id,
                                                              IrrigationCommand.status.in_(ACTIVE))
                              .order_by(IrrigationCommand.created_at.desc())).first()
        last_done = session.exec(select(IrrigationCommand).where(IrrigationCommand.zone_id == zone.id,
                                                                 IrrigationCommand.status.in_(("done", "aborted")))
                                 .order_by(IrrigationCommand.finished_at.desc())).first()
        fc, wp, _ = services.zone_soil(zone)
        try:
            plan = build_plan(session, zone, now)
        except Exception as exc:                      # the page must still load
            plan = {"status": "insufficient_data", "missing": [f"plan error: {exc}"]}
        stage = growth.assess(session, zone, today=today) if profile and zone.planting_date else {"available": False}
        vision = _vision_summary(session, zone)
        zones_out.append({
            "id": zone.id, "name": zone.name, "valve_channel": zone.valve_channel, "strategy": zone.strategy,
            "area_m2": zone.area_m2, "identification_status": zone.identification_status,
            "crop": {"key": profile.key, "name": profile.name} if profile else None,
            "planting_date": zone.planting_date.isoformat() if zone.planting_date else None,
            "soil": {"type": zone.soil_type, "theta_fc": fc, "theta_wp": wp},
            "stage": stage,
            "sensors": _sensor_block(zone, zr, cr, now, fc, wp),
            "decision": {"action": decision.action, "reasons": decision.reasons, "ts": _iso(decision.ts),
                         "summary_fr": decision_fr(decision, settings.timezone),
                         "values": decision.values} if decision else None,
            "active_command": _command(active), "last_command": _command(last_done),
            "plan": {k: v for k, v in plan.items() if k not in ("daily",)},
            "plan_daily": plan.get("daily", []),
            "flow": _flow_steps(zone, ctrl_online, zr, decision, plan, active, last_done, cr, anomalies,
                                state.emergency_stop, now, wt),
            "anomalies": [_anomaly(a, zone_names) for a in anomalies if a.zone_id in (zone.id, None)],
            "vision": vision,
            "procedure": _procedure(zone, zr, vision, decision, active, last_done, ctrl_online,
                                    state.emergency_stop, plan, now),
        })
    return {
        "now": _iso(now), "timezone": settings.timezone,
        "system": {"mode": state.mode, "emergency_stop": state.emergency_stop,
                   "controller_last_seen": _iso(state.controller_last_seen), "controller_age_s": ctrl_age,
                   "controller_online": ctrl_online,
                   "valves_open": (cr.valves_open or []) if cr else [], "tank_level_pct": cr.tank_level_pct if cr else None},
        "weather_today": {"et0_mm": wt.et0_mm, "precip_mm": wt.precip_mm, "precip_probability_pct": wt.precip_probability_pct,
                          "tmax_c": wt.tmax_c, "tmin_c": wt.tmin_c, "source": wt.source, "kind": "forecast"} if wt else None,
        "anomalies": [_anomaly(a, zone_names) for a in anomalies],
        "vision_url": settings.vision_public_url,
        "zones": zones_out,
    }


def _stage_changes(rows: list[StageObservation]) -> list[dict[str, Any]]:
    """Stage changes to mark on the chart: calendar transitions and farmer confirmations, one per change."""
    out, last = [], None
    for s in rows:
        if not s.stage_key or s.source == "ai" or s.status in ("rejected", "superseded"):
            continue
        if s.stage_key != last:
            out.append({"ts": _iso(s.ts), "stage": s.stage_key, "source": s.source, "status": s.status})
            last = s.stage_key
    return out


@router.get("/zones/{zone_id}/history")
def zone_history(zone_id: int, hours: int = 72, session: Session = Depends(get_session)) -> dict[str, Any]:
    zone = session.get(Zone, zone_id)
    if zone is None:
        raise HTTPException(404, "zone not found")
    now = utcnow()
    since = now - timedelta(hours=max(6, min(hours, 24 * 30)))
    readings = session.exec(select(SensorReading).where(SensorReading.zone_id == zone_id, SensorReading.ts >= since)
                            .order_by(SensorReading.ts)).all()
    ctrl = session.exec(select(SensorReading).where(SensorReading.zone_id.is_(None), SensorReading.ts >= since)
                        .order_by(SensorReading.ts)).all()
    cmds = session.exec(select(IrrigationCommand).where(IrrigationCommand.zone_id == zone_id,
                                                        IrrigationCommand.created_at >= since)
                        .order_by(IrrigationCommand.created_at)).all()
    decisions = session.exec(select(Decision).where(Decision.zone_id == zone_id, Decision.ts >= since)
                             .order_by(Decision.ts)).all()
    stages = session.exec(select(StageObservation).where(StageObservation.zone_id == zone_id,
                                                         StageObservation.ts >= since)
                          .order_by(StageObservation.ts)).all()
    fc, wp, _ = services.zone_soil(zone)
    return {
        "from": _iso(since), "to": _iso(now), "theta_fc": fc, "theta_wp": wp,
        "soil": [{"ts": _iso(r.ts), "theta": r.soil_theta, "valid": r.valid} for r in readings if r.soil_theta is not None],
        "air_temp": [{"ts": _iso(r.ts), "value": r.air_temp_c} for r in ctrl if r.air_temp_c is not None],
        "irrigations": [{"ts": _iso(c.started_at or c.created_at), "end": _iso(c.finished_at), "status": c.status,
                         "target_l": c.target_volume_l, "delivered_l": c.delivered_l, "origin": c.origin} for c in cmds],
        "needs": [{"ts": _iso(d.ts), "etc_mm": _v(d, "etc"), "depletion_mm": _v(d, "depletion"), "raw_mm": _v(d, "raw"),
                   "root_depth_m": _v(d, "root_depth"), "action": d.action} for d in decisions],
        "stage_changes": _stage_changes(stages),
    }


# ---------------------------------------------------------------- camera procedure
VISUAL_FR = {"seedling": "jeune plant", "leafy_no_flowers": "feuillage, sans fleurs", "flowers": "en fleurs",
             "green_fruits": "fruits verts", "coloured_fruits": "fruits colorés", "emergence": "levée",
             "leafy_canopy": "feuillage couvrant", "flowering": "floraison", "senescence": "sénescence",
             "fruits": "fruits"}
CROP_FR = {"tomato": "Tomate", "pepper": "Poivron", "potato": "Pomme de terre", "eggplant": "Aubergine",
           "other": "Autre", "unknown": "Incertaine"}


def _nearest(session: Session, zone_id: Optional[int], ts: datetime, minutes: int = 30) -> Optional[SensorReading]:
    q = select(SensorReading).where(SensorReading.ts >= ts - timedelta(minutes=minutes),
                                    SensorReading.ts <= ts + timedelta(minutes=minutes))
    q = q.where(SensorReading.zone_id == zone_id) if zone_id is not None else q.where(SensorReading.zone_id.is_(None))
    rows = session.exec(q).all()
    return min(rows, key=lambda r: abs((r.ts - ts).total_seconds())) if rows else None


def camera_steps(session: Session, obs: VisionObservation, now: datetime, with_current: bool) -> dict[str, Any]:
    p = obs.payload or {}
    zone = session.get(Zone, obs.zone_id) if obs.zone_id else None
    profile = session.get(CropProfile, zone.crop_profile_id) if zone and zone.crop_profile_id else None
    crop = p.get("crop") or {}
    stage = p.get("stage") or {}
    health = p.get("health") or {}
    so = session.exec(select(StageObservation).where(StageObservation.vision_observation_id == obs.id)).first()
    zr = _nearest(session, obs.zone_id, obs.ts) if obs.zone_id else None
    cr = _nearest(session, None, obs.ts)
    steps = []
    # 1 photo
    steps.append({"key": "photo", "title": "Photo reçue", "state": "ok",
                  "value": {"phone": "Téléphone", "mast": "Caméra fixe", "carriage": "Chariot", "trap": "Piège"}.get(obs.source, obs.source),
                  "detail": (zone.name if zone else "Sans zone") + (f", plante {obs.plant_ref}" if obs.plant_ref else ""),
                  "kind": "measured"})
    if obs.source == "trap":
        n = (p.get("trap") or {}).get("insect_count")
        steps.append({"key": "trap", "title": "Comptage du piège", "state": "ok" if n is not None else "warn",
                      "value": f"{n} insectes" if n is not None else "Piège non trouvé", "kind": "measured",
                      "detail": "Comptage par vision classique (pas d'IA)."})
        return {"steps": steps}
    # 2 crop
    ck = crop.get("key")
    if not ck:
        steps.append({"key": "crop", "title": "Culture", "state": "error", "value": "Non identifiée", "detail": ""})
    elif ck == "unknown":
        steps.append({"key": "crop", "title": "Culture", "state": "warn", "value": "Incertaine", "kind": "ai",
                      "detail": f"Le modèle hésite (meilleure hypothèse : {crop.get('best_guess', '?')}). "
                                "Diagnostic incertain — confirmer la culture."})
    else:
        match = profile is None or profile.key == ck
        steps.append({"key": "crop", "title": "Culture", "state": "ok" if match else "warn", "kind": "ai",
                      "value": CROP_FR.get(ck, crop.get("label", ck)), "confidence": crop.get("confidence"),
                      "detail": ("Conforme à la culture enregistrée de la zone." if match and profile else
                                 f"La zone est enregistrée en {profile.name} : confirmer la culture." if profile else
                                 "Aucune culture enregistrée pour cette zone.")})
    # 3 visual stage
    if stage.get("key"):
        steps.append({"key": "visual_stage", "title": "Stade visuel", "state": "ok", "kind": "ai",
                      "value": VISUAL_FR.get(stage["key"], stage["key"].replace("_", " ")),
                      "confidence": stage.get("confidence"),
                      "detail": ("Description reconnue par le modèle : " + ", ".join(stage["visual_cues"]))
                      if stage.get("visual_cues") else ""})
    else:
        steps.append({"key": "visual_stage", "title": "Stade visuel", "state": "warn", "value": "Non évalué",
                      "detail": "Pas de stade sans culture identifiée."})
    # 4 health
    pests = p.get("pests") or []
    if pests:
        steps.append({"key": "health", "title": "Santé", "state": "warn", "kind": "ai",
                      "value": ", ".join(x["label"].replace("_", " ") for x in pests),
                      "detail": "Détecteur entraîné : vérifier sur la plante."})
    elif health:
        steps.append({"key": "health", "title": "Santé", "state": "ok" if health.get("hint") == "healthy" else "warn",
                      "kind": "ai_hint", "value": "Rien de visible" if health.get("hint") == "healthy" else "À inspecter",
                      "detail": health.get("message_fr", "")})
    else:
        steps.append({"key": "health", "title": "Santé", "state": "idle", "value": "Non évaluée", "detail": ""})
    # 5 sensors at the same moment
    parts, state = [], "ok"
    if zr is not None and zr.soil_theta is not None:
        parts.append(f"sol {zr.soil_theta * 100:.1f} % vol.")
    else:
        state = "warn"
        parts.append("pas d'humidité du sol à ±30 min")
    if cr is not None:
        if cr.air_temp_c is not None:
            parts.append(f"air {cr.air_temp_c:.1f} °C")
        if cr.rain_sensor_wet is not None:
            parts.append("pluie détectée" if cr.rain_sensor_wet else "sec")
    else:
        state = "warn"
        parts.append("aucune donnée de l'ESP32")
    rain = p.get("rain") or {}
    if rain.get("source") == "rain sensor":
        parts.append("pluie : capteur prioritaire sur la caméra")
    steps.append({"key": "sensors", "title": "Capteurs ESP32 au même moment", "state": state, "kind": "measured",
                  "value": "Croisement OK" if state == "ok" else "Données partielles", "detail": ", ".join(parts)})
    # current assessment of the zone (used by steps 6-8 for the latest photo)
    cur = None
    if with_current and zone is not None and profile is not None and ck == profile.key and zone.planting_date:
        cur = growth.assess(session, zone, today=services.local_today(now, settings))
    disagreement = bool(cur and cur.get("available") and cur.get("agreement") == "disagreement" and so is not None
                        and so.status in ("estimated", "needs_confirmation"))
    # 6 stage retained (calendar + degree-days + photo + farmer)
    if so is not None:
        cands = so.candidate_stages or []
        cyc = growth.cycle_of(profile) if profile else None
        names = {c: (cyc.get(c).name_fr if cyc and c in [x.key for x in cyc.stages] else c) for c in cands}
        status = "needs_confirmation" if disagreement else so.status
        detail = "Stades compatibles avec la photo : " + (", ".join(names.values()) or "—")
        if disagreement:
            detail += f". Le calendrier indique : {cur['stage']['name_fr']}."
        elif cur and cur.get("available") and so.status in ("estimated",):
            detail += f". Stade retenu pour la zone : {cur['stage']['name_fr']}."
        steps.append({"key": "stage", "title": "Stade retenu", "kind": "calculated", "candidate_names": names,
                      "state": {"confirmed": "ok", "rejected": "idle", "superseded": "idle",
                                "needs_confirmation": "warn"}.get(status, "ok"),
                      "value": {"confirmed": "Confirmé", "rejected": "Rejeté", "needs_confirmation": "À confirmer",
                                "estimated": "Estimé", "superseded": "Remplacé par votre confirmation"}.get(status, status),
                      "detail": detail, "stage_observation_id": so.id, "candidates": cands, "status": status,
                      "zone_id": obs.zone_id,
                      "calendar_stage": ({"key": cur["stage"]["key"], "name_fr": cur["stage"]["name_fr"]}
                                         if disagreement else None)})
    else:
        steps.append({"key": "stage", "title": "Stade retenu", "state": "idle", "value": "Photo non utilisée",
                      "detail": "Photo d'une autre culture, confiance trop faible ou zone sans date de plantation."})
    out: dict[str, Any] = {"steps": steps}
    # 7 irrigation plan (current, not at the time of the photo)
    if with_current and zone is not None and profile is not None and ck in (profile.key,):
        out["current_stage"] = cur or {"available": False}
        try:
            out["current_plan"] = plan_summary(build_plan(session, zone, now))
        except Exception as exc:
            out["current_plan"] = {"status": "insufficient_data", "missing": [str(exc)]}
        pl = out["current_plan"]
        if pl.get("next_irrigation"):
            pdet = f"Prochaine : {pl['next_irrigation']['when_fr']} ({pl['next_irrigation']['volume_l']} L)"
        elif pl.get("missing"):
            pdet = "Manquant : " + "; ".join(pl["missing"])
        else:
            pdet = pl.get("status_text_fr") or ""
        steps.append({"key": "plan", "title": "Plan d'irrigation", "kind": "calculated",
                      "state": "ok" if pl.get("frequency") else "warn",
                      "value": (pl.get("frequency") or {}).get("text_fr", "Données insuffisantes"), "detail": pdet})
    else:
        steps.append({"key": "plan", "title": "Plan d'irrigation", "state": "idle", "value": "Non calculé",
                      "detail": "Le plan n'est donné que si la culture de la photo est celle de la zone."})
    # 8 farmer action
    needs = so is not None and (so.status == "needs_confirmation" or disagreement)
    steps.append({"key": "farmer", "title": "Action de l'agriculteur", "state": "warn" if needs else "ok",
                  "value": "Confirmer le stade" if needs else "Rien à faire",
                  "detail": "Le stade proposé diffère du calendrier ou n'est pas visible : confirmer ou corriger."
                  if needs else ""})
    return out


@router.get("/camera")
def camera(zone_id: Optional[int] = None, limit: int = 20, session: Session = Depends(get_session)) -> dict[str, Any]:
    now = utcnow()
    q = select(VisionObservation).order_by(VisionObservation.ts.desc()).limit(max(1, min(limit, 100)))
    if zone_id is not None:
        q = q.where(VisionObservation.zone_id == zone_id)
    rows = session.exec(q).all()
    items = []
    for i, o in enumerate(rows):
        p = o.payload or {}
        item = {"id": o.id, "ts": _iso(o.ts), "zone_id": o.zone_id, "plant_ref": o.plant_ref, "source": o.source,
                "image_ref": o.image_ref, "crop_key": o.crop_key, "crop_confidence": o.crop_confidence,
                "crop_fr": CROP_FR.get(o.crop_key) if o.crop_key else None,
                "growth_stage": o.growth_stage,
                "growth_stage_fr": VISUAL_FR.get(o.growth_stage) if o.growth_stage else None, "health": p.get("health"), "pests": p.get("pests") or [],
                "animals": p.get("animals") or [], "trap": p.get("trap"), "rain": p.get("rain"),
                "canopy": p.get("canopy"), "engines": o.engines}
        item.update(camera_steps(session, o, now, with_current=(i == 0)))
        items.append(item)
    state = services.system_state(session)
    ctrl_age = _age_s(state.controller_last_seen, now)
    # cameras = (source, zone) pairs that sent images, with their last image and their rhythm
    recent = session.exec(select(VisionObservation).where(VisionObservation.source != "phone")
                          .order_by(VisionObservation.ts.desc()).limit(500)).all()
    cams: dict[tuple, dict[str, Any]] = {}
    for o in recent:
        k = (o.source, o.zone_id)
        c = cams.setdefault(k, {"source": o.source, "zone_id": o.zone_id, "last_ts": _iso(o.ts), "last_id": o.id,
                                "last_image_ref": o.image_ref, "age_s": _age_s(o.ts, now), "_ts": []})
        if len(c["_ts"]) < 6:
            c["_ts"].append(o.ts)
    cameras = []
    for c in cams.values():
        ts = c.pop("_ts")
        gaps = [(a - b).total_seconds() for a, b in zip(ts, ts[1:])]
        interval = sorted(gaps)[len(gaps) // 2] if gaps else None          # median gap = capture rhythm
        c["interval_s"] = round(interval) if interval else None
        late = c["age_s"] is not None and interval and c["age_s"] > max(2.5 * interval, 120)
        c["state"] = "late" if late else "ok" if interval else "single"
        cameras.append(c)
    cameras.sort(key=lambda c: (c["zone_id"] or 0, c["source"]))
    return {"now": _iso(now), "vision_url": settings.vision_public_url, "cameras": cameras,
            "controller_online": ctrl_age is not None and ctrl_age <= CONTROLLER_OFFLINE_MIN * 60,
            "zones": [{"id": z.id, "name": z.name,
                       "crop": (session.get(CropProfile, z.crop_profile_id).name if z.crop_profile_id else None)}
                      for z in session.exec(select(Zone).order_by(Zone.id)).all()],
            "observations": items}


@router.get("/camera/{obs_id}")
def camera_one(obs_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    o = session.get(VisionObservation, obs_id)
    if o is None:
        raise HTTPException(404, "observation not found")
    now = utcnow()
    return {"id": o.id, "ts": _iso(o.ts), "zone_id": o.zone_id, "image_ref": o.image_ref, "source": o.source,
            **camera_steps(session, o, now, with_current=True)}


# ---------------------------------------------------------------- farm map layout
class LayoutIn(BaseModel):
    data: dict[str, Any]


def _default_layout(zones: list[Zone]) -> dict[str, Any]:
    """Tank on the left, plots in two columns on the right (world units, the map scales them)."""
    items = {"tank": {"x": 40, "y": 120}, "esp32": {"x": 20, "y": 400}}
    for i, z in enumerate(zones):
        items[f"zone:{z.id}"] = {"x": 420 + (i % 2) * 360, "y": 40 + (i // 2) * 240}
    return {"items": items, "version": 1}


@router.get("/layout")
def get_layout(session: Session = Depends(get_session)) -> dict[str, Any]:
    zones = list(session.exec(select(Zone).order_by(Zone.id)).all())
    row = session.get(DashboardLayout, 1)
    layout = _default_layout(zones)
    saved = (row.data or {}).get("items", {}) if row else {}
    for k, v in saved.items():                       # saved positions win; new zones get a default place
        if k in layout["items"] and isinstance(v, dict) and "x" in v and "y" in v:
            layout["items"][k] = {"x": float(v["x"]), "y": float(v["y"])}
    layout["saved"] = bool(row)
    return layout


@router.put("/layout")
def put_layout(body: LayoutIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    items = {k: {"x": float(v["x"]), "y": float(v["y"])} for k, v in (body.data.get("items") or {}).items()
             if isinstance(v, dict) and "x" in v and "y" in v}
    row = session.get(DashboardLayout, 1) or DashboardLayout(id=1)
    row.data, row.updated_at = {"items": items, "version": 1}, utcnow()
    session.add(row)
    session.commit()
    return {"saved": True, "items": len(items)}


@router.delete("/layout")
def reset_layout(session: Session = Depends(get_session)) -> dict[str, Any]:
    row = session.get(DashboardLayout, 1)
    if row:
        session.delete(row)
        session.commit()
    return {"reset": True}
