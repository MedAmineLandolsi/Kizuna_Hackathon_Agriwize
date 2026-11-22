"""Glue between the database, the agronomy core and the decision engine."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from . import growth
from .agronomy import kc as kcmod
from .agronomy import water_balance as wb
from .agronomy.reference_data import EFFICIENCY_SOURCE, IRRIGATION_EFFICIENCY, SOILS
from .config import Settings, settings as default_settings
from .decision import CropInputs, DecisionInputs, DecisionResult, decide
from .models import (Anomaly, CropProfile, Decision, Identification, IrrigationCommand, SensorReading,
                     SystemState, VisionObservation, WeatherDay, Zone, utcnow)
from .sensors import raw_to_theta, validate_reading
from .weather import weather_for_day

ACTIVE_STATUSES = ("pending", "sent", "running", "abort_requested")
COMMAND_EXPIRY = timedelta(hours=2)


# ---------------------------------------------------------------- helpers
def system_state(session: Session) -> SystemState:
    state = session.get(SystemState, 1)
    if state is None:
        state = SystemState(id=1)
        session.add(state)
        session.commit()
        session.refresh(state)
    return state


def local_today(now_utc: datetime, cfg: Settings) -> date:
    return now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(cfg.timezone)).date()


def zone_soil(zone: Zone) -> tuple[float, float, str]:
    ref = SOILS.get(zone.soil_type, SOILS["loam"])
    fc = zone.theta_fc if zone.theta_fc is not None else ref.theta_fc
    wp = zone.theta_wp if zone.theta_wp is not None else ref.theta_wp
    src = "field measurement" if zone.theta_fc is not None else f"{ref.name}, {ref.source}"
    return fc, wp, src


def zone_efficiency(zone: Zone) -> tuple[float, str]:
    if zone.efficiency:
        return zone.efficiency, "configured for this zone"
    return IRRIGATION_EFFICIENCY.get(zone.irrigation_method, 0.75), EFFICIENCY_SOURCE


def crop_inputs(profile: CropProfile | None) -> CropInputs | None:
    if profile is None:
        return None
    return CropInputs(
        name=profile.name,
        curve=kcmod.KcCurve(profile.kc_ini, profile.kc_mid, profile.kc_end,
                            profile.l_ini, profile.l_dev, profile.l_mid, profile.l_late),
        max_height_m=profile.max_height_m, zr_ini_m=profile.zr_ini_m, zr_max_m=profile.zr_max_m,
        p=profile.p, source=profile.source or "crop profile",
    )


def latest_zone_reading(session: Session, zone_id: int) -> SensorReading | None:
    return session.exec(select(SensorReading).where(SensorReading.zone_id == zone_id)
                        .order_by(SensorReading.ts.desc())).first()


def latest_controller_reading(session: Session) -> SensorReading | None:
    return session.exec(select(SensorReading).where(SensorReading.zone_id.is_(None))
                        .order_by(SensorReading.ts.desc())).first()


def open_anomalies(session: Session, zone_id: int | None = None, blocking_only: bool = False) -> list[Anomaly]:
    q = select(Anomaly).where(Anomaly.acknowledged == False)  # noqa: E712
    if blocking_only:
        q = q.where(Anomaly.blocks_irrigation == True)  # noqa: E712
    rows = session.exec(q.order_by(Anomaly.ts.desc())).all()
    if zone_id is None:
        return list(rows)
    return [a for a in rows if a.zone_id in (zone_id, None)]


def raise_anomaly(session: Session, *, zone_id: int | None, kind: str, message: str,
                  severity: str = "warning", blocks: bool = True) -> Anomaly:
    """Create an anomaly unless the same unacknowledged one already exists."""
    existing = session.exec(select(Anomaly).where(Anomaly.zone_id == zone_id, Anomaly.kind == kind,
                                                  Anomaly.acknowledged == False)).first()  # noqa: E712
    if existing:
        return existing
    a = Anomaly(zone_id=zone_id, kind=kind, message=message, severity=severity, blocks_irrigation=blocks)
    session.add(a)
    session.commit()
    session.refresh(a)
    return a


def active_command(session: Session, zone_id: int) -> IrrigationCommand | None:
    return session.exec(select(IrrigationCommand).where(IrrigationCommand.zone_id == zone_id,
                                                        IrrigationCommand.status.in_(ACTIVE_STATUSES))).first()


# ---------------------------------------------------------------- readings
def ingest_readings(session: Session, payload: dict[str, Any], now: datetime | None = None,
                    cfg: Settings = default_settings) -> dict[str, Any]:
    now = now or utcnow()
    state = system_state(session)
    state.controller_last_seen = now
    session.add(state)
    stored: list[dict[str, Any]] = []

    ctrl = payload.get("controller") or {}
    if ctrl:
        r = SensorReading(ts=now, zone_id=None, air_temp_c=ctrl.get("air_temp_c"), air_rh_pct=ctrl.get("air_rh_pct"),
                          tank_level_pct=ctrl.get("tank_level_pct"), flow_lpm=ctrl.get("flow_lpm"),
                          valves_open=ctrl.get("valves_open"), rain_sensor_wet=ctrl.get("rain_sensor_wet"))
        v = validate_reading(r)
        r.valid, r.issues = v.valid, v.issues
        session.add(r)
        stored.append({"zone_id": None, "valid": v.valid, "issues": v.issues})
        valves = ctrl.get("valves_open") or []
        flow = ctrl.get("flow_lpm") or 0.0
        if not valves and flow > cfg.leak_flow_threshold_lpm:
            raise_anomaly(session, zone_id=None, kind="leak", severity="critical",
                          message=f"Flow of {flow:.2f} L/min measured while all valves are closed (leak or stuck valve).")

    zones_by_channel = {z.valve_channel: z for z in session.exec(select(Zone)).all()}
    for zr in payload.get("zones", []):
        zone = session.get(Zone, zr["zone_id"]) if zr.get("zone_id") else zones_by_channel.get(zr.get("valve_channel"))
        if zone is None:
            stored.append({"error": f"unknown zone {zr}"})
            continue
        theta = zr.get("soil_theta")
        if theta is None and zr.get("soil_raw") is not None:
            theta = raw_to_theta(zone, zr["soil_raw"])
        r = SensorReading(ts=now, zone_id=zone.id, soil_raw=zr.get("soil_raw"), soil_theta=theta,
                          soil_temp_c=zr.get("soil_temp_c"), soil_ec_ds_m=zr.get("soil_ec_ds_m"))
        v = validate_reading(r)
        if zr.get("soil_raw") is not None and theta is None:
            v.valid = False
            v.issues.append("soil sensor not calibrated for this zone")
        r.valid, r.issues = v.valid, v.issues
        session.add(r)
        if not v.valid:
            raise_anomaly(session, zone_id=zone.id, kind="sensor", blocks=False,
                          message=f"{zone.name}: invalid sensor reading ({'; '.join(v.issues)}). "
                                  "Automatic irrigation suspended for this zone until readings are valid.")
        stored.append({"zone_id": zone.id, "soil_theta": theta, "valid": v.valid, "issues": v.issues})
    session.commit()
    return {"stored": stored, "emergency_stop": state.emergency_stop}


# ---------------------------------------------------------------- decisions
def build_inputs(session: Session, zone: Zone, now: datetime, cfg: Settings) -> DecisionInputs:
    state = system_state(session)
    profile = session.get(CropProfile, zone.crop_profile_id) if zone.crop_profile_id else None
    fc, wp, soil_src = zone_soil(zone)
    eff, eff_src = zone_efficiency(zone)
    reading = latest_zone_reading(session, zone.id)
    ctrl = latest_controller_reading(session)
    today = local_today(now, cfg)
    w_today = weather_for_day(session, today, now)
    from .models import WeatherDay  # local import to avoid a cycle in type checkers
    w_tomorrow = session.exec(select(WeatherDay).where(WeatherDay.day == today + timedelta(days=1))).first()
    tank = None
    if ctrl and ctrl.valid and (now - ctrl.ts) <= timedelta(minutes=cfg.reading_max_age_min):
        tank = ctrl.tank_level_pct
    raining, rain_src = raining_now(session, zone.id, now, cfg)
    stage_info = growth.assess(session, zone, today=local_today(now, cfg)) if zone.planting_date else None
    return DecisionInputs(
        now_utc=now, timezone=cfg.timezone, zone_name=zone.name, zone_enabled=zone.enabled,
        strategy=zone.strategy, planting_date=zone.planting_date, crop=crop_inputs(profile),
        identification_status=zone.identification_status,
        theta_fc=fc, theta_wp=wp, soil_source=soil_src, root_depth_override_m=zone.root_depth_override_m,
        area_m2=zone.area_m2, efficiency=eff, efficiency_source=eff_src, flow_rate_lpm=zone.flow_rate_lpm,
        refill_fraction=zone.refill_fraction,
        soil_theta=reading.soil_theta if reading else None,
        soil_reading_age_min=(now - reading.ts).total_seconds() / 60 if reading else None,
        soil_reading_issues=list(reading.issues) if reading else [],
        tank_level_pct=tank,
        weather_today=w_today,
        rain_tomorrow_mm=w_tomorrow.precip_mm if w_tomorrow else None,
        rain_tomorrow_probability_pct=w_tomorrow.precip_probability_pct if w_tomorrow else None,
        mode=state.mode, emergency_stop=state.emergency_stop,
        blocking_anomalies=[a.message for a in open_anomalies(session, zone.id, blocking_only=True)],
        active_command=active_command(session, zone.id) is not None,
        irrigation_windows=cfg.irrigation_windows,
        rain_postpone_probability_pct=cfg.rain_postpone_probability_pct,
        reading_max_age_min=cfg.reading_max_age_min, min_tank_level_pct=cfg.min_tank_level_pct,
        max_volume_per_event_l=cfg.max_volume_per_event_l, timeout_factor=cfg.timeout_factor,
        raining_now=raining, raining_now_source=rain_src,
        effective_day=stage_info["effective_day"] if stage_info and stage_info.get("available") else None,
        stage_info=stage_info,
    )


def run_decision(session: Session, zone: Zone, now: datetime | None = None,
                 cfg: Settings = default_settings) -> Decision:
    now = now or utcnow()
    inputs = build_inputs(session, zone, now, cfg)
    if inputs.stage_info and inputs.stage_info.get("available"):
        growth.record_transition(session, zone, inputs.stage_info)
    result: DecisionResult = decide(inputs)
    d = Decision(zone_id=zone.id, ts=now, action=result.action, reasons=result.reasons, values=result.values,
                 target_volume_l=result.target_volume_l, scheduled_for=result.scheduled_for_utc)
    session.add(d)
    session.commit()
    session.refresh(d)
    if result.action in ("irrigate_now", "schedule"):
        cmd = IrrigationCommand(zone_id=zone.id, decision_id=d.id, origin="auto", created_at=now,
                                scheduled_for=result.scheduled_for_utc or now,
                                target_volume_l=result.target_volume_l, timeout_s=result.timeout_s or 600,
                                theta_before=inputs.soil_theta)
        session.add(cmd)
        session.commit()
        session.refresh(cmd)
        d.command_id = cmd.id
        session.add(d)
        session.commit()
        session.refresh(d)
    return d


def run_all_decisions(session: Session, now: datetime | None = None, cfg: Settings = default_settings) -> list[Decision]:
    out = []
    for zone in session.exec(select(Zone).where(Zone.strategy == "adaptive")).all():
        if active_command(session, zone.id):
            continue  # one command at a time per zone; re-evaluated after it finishes
        out.append(run_decision(session, zone, now, cfg))
    for d in out:  # later commits expired the earlier objects; reload them for the response
        session.refresh(d)
    return out


def run_fixed_schedules(session: Session, now: datetime | None = None, cfg: Settings = default_settings) -> list[IrrigationCommand]:
    """Control zones: fixed volume at a fixed local time, like a conventional timer."""
    now = now or utcnow()
    state = system_state(session)
    created = []
    local_now = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(cfg.timezone))
    for zone in session.exec(select(Zone).where(Zone.strategy == "fixed", Zone.enabled == True)).all():  # noqa: E712
        if state.emergency_stop or state.mode != "auto" or zone.fixed_volume_l <= 0:
            continue
        hh, mm = (int(x) for x in zone.fixed_time.split(":"))
        due_local = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if local_now < due_local or active_command(session, zone.id):
            continue
        day_start_utc = (local_now.replace(hour=0, minute=0, second=0, microsecond=0)
                         .astimezone(ZoneInfo("UTC")).replace(tzinfo=None))
        already = session.exec(select(IrrigationCommand).where(
            IrrigationCommand.zone_id == zone.id, IrrigationCommand.origin == "fixed",
            IrrigationCommand.created_at >= day_start_utc)).first()
        if already or open_anomalies(session, zone.id, blocking_only=True):
            continue
        reading = latest_zone_reading(session, zone.id)
        duration_min = zone.fixed_volume_l / zone.flow_rate_lpm if zone.flow_rate_lpm > 0 else 10
        cmd = IrrigationCommand(zone_id=zone.id, origin="fixed", created_at=now, scheduled_for=now,
                                target_volume_l=zone.fixed_volume_l,
                                timeout_s=int(max(duration_min * 60 * cfg.timeout_factor, 60)),
                                theta_before=reading.soil_theta if reading and reading.valid else None)
        session.add(cmd)
        session.commit()
        session.refresh(cmd)
        created.append(cmd)
    return created


# ---------------------------------------------------------------- controller protocol
def _command_payload(cmd: IrrigationCommand, session: Session, action: str = "irrigate") -> dict[str, Any]:
    zone = session.get(Zone, cmd.zone_id)
    return {"action": action, "command_id": cmd.id, "zone_id": cmd.zone_id, "valve_channel": zone.valve_channel,
            "target_volume_l": cmd.target_volume_l, "timeout_s": cmd.timeout_s}


def next_controller_command(session: Session, now: datetime | None = None,
                            cfg: Settings = default_settings) -> dict[str, Any]:
    now = now or utcnow()
    state = system_state(session)
    state.controller_last_seen = now
    session.add(state)
    session.commit()
    if state.emergency_stop:
        return {"action": "stop_all", "reason": "emergency_stop"}

    abort = session.exec(select(IrrigationCommand).where(IrrigationCommand.status == "abort_requested")).first()
    if abort:
        return _command_payload(abort, session, action="stop")

    current = session.exec(select(IrrigationCommand).where(IrrigationCommand.status.in_(("sent", "running")))).first()
    if current:  # idempotent: the controller may poll again while irrigating
        return _command_payload(current, session)

    pending = session.exec(select(IrrigationCommand).where(IrrigationCommand.status == "pending",
                                                           IrrigationCommand.scheduled_for <= now)
                           .order_by(IrrigationCommand.scheduled_for)).all()
    for cmd in pending:
        if now - cmd.scheduled_for > COMMAND_EXPIRY:
            cmd.status, cmd.stop_reason = "cancelled", "expired (data too old)"
            session.add(cmd)
            continue
        # Re-validate safety at dispatch time
        blockers = [a.message for a in open_anomalies(session, cmd.zone_id, blocking_only=True)]
        ctrl = latest_controller_reading(session)
        if ctrl and ctrl.valid and ctrl.tank_level_pct is not None and ctrl.tank_level_pct < cfg.min_tank_level_pct:
            blockers.append("tank level below minimum")
        if state.mode != "auto" and cmd.origin != "manual":
            blockers.append("manual mode")
        if blockers:
            cmd.status, cmd.stop_reason = "cancelled", "blocked at dispatch: " + "; ".join(blockers)
            session.add(cmd)
            continue
        cmd.status, cmd.sent_at = "sent", now
        session.add(cmd)
        session.commit()
        return _command_payload(cmd, session)
    session.commit()
    return {"action": "idle"}


def mark_started(session: Session, cmd: IrrigationCommand, now: datetime | None = None) -> IrrigationCommand:
    cmd.status, cmd.started_at = "running", now or utcnow()
    session.add(cmd)
    session.commit()
    session.refresh(cmd)
    return cmd


def report_finished(session: Session, cmd: IrrigationCommand, delivered_l: float, stop_reason: str,
                    now: datetime | None = None, cfg: Settings = default_settings) -> IrrigationCommand:
    """Post-irrigation verification, step 1: delivered vs target volume (flowmeter)."""
    now = now or utcnow()
    zone = session.get(Zone, cmd.zone_id)
    cmd.delivered_l, cmd.stop_reason, cmd.finished_at = delivered_l, stop_reason, now
    cmd.status = "done" if stop_reason == "volume_reached" else "aborted"
    target = cmd.target_volume_l
    error = (delivered_l - target) / target if target > 0 else 0.0
    check = {"delivered_l": delivered_l, "target_l": target, "relative_error": round(error, 4),
             "tolerance": cfg.delivery_tolerance, "volume_ok": abs(error) <= cfg.delivery_tolerance,
             "moisture_check_due": (now + timedelta(minutes=cfg.moisture_check_delay_min)).isoformat()}
    if stop_reason not in ("manual_stop", "emergency_stop"):
        if error < -cfg.delivery_tolerance:
            raise_anomaly(session, zone_id=zone.id, kind="low_delivery", severity="critical",
                          message=f"{zone.name}: only {delivered_l:.1f} L of {target:.1f} L delivered "
                                  f"({stop_reason}). Possible blocked pipe, closed manual valve, pump or pressure problem.")
        elif error > cfg.delivery_tolerance:
            raise_anomaly(session, zone_id=zone.id, kind="over_delivery", severity="critical",
                          message=f"{zone.name}: {delivered_l:.1f} L delivered for a {target:.1f} L target. "
                                  "Possible stuck valve or flowmeter fault.")
    cmd.verification = check
    session.add(cmd)
    session.commit()
    session.refresh(cmd)
    return cmd


def check_moisture_responses(session: Session, now: datetime | None = None, cfg: Settings = default_settings) -> int:
    """Post-irrigation verification, step 2: did soil moisture rise as expected?"""
    now = now or utcnow()
    done = 0
    cmds = session.exec(select(IrrigationCommand).where(IrrigationCommand.status.in_(("done", "aborted")),
                                                        IrrigationCommand.theta_after.is_(None))).all()
    for cmd in cmds:
        due = cmd.finished_at + timedelta(minutes=cfg.moisture_check_delay_min) if cmd.finished_at else None
        if due is None or now < due or not cmd.delivered_l:
            continue
        reading = session.exec(select(SensorReading).where(SensorReading.zone_id == cmd.zone_id,
                                                           SensorReading.ts >= due, SensorReading.valid == True)  # noqa: E712
                               .order_by(SensorReading.ts)).first()
        if reading is None or reading.soil_theta is None:
            continue
        zone = session.get(Zone, cmd.zone_id)
        profile = session.get(CropProfile, zone.crop_profile_id) if zone.crop_profile_id else None
        fc, _, _ = zone_soil(zone)
        eff, _ = zone_efficiency(zone)
        if profile:
            dap = (local_today(now, cfg) - zone.planting_date).days if zone.planting_date else 0
            zr = zone.root_depth_override_m or kcmod.root_depth_for_day(
                profile.zr_ini_m, profile.zr_max_m, crop_inputs(profile).curve, dap)
        else:
            zr = zone.root_depth_override_m or 0.3
        net_mm = wb.litres_to_mm(cmd.delivered_l, zone.area_m2) * eff
        expected = net_mm / (1000.0 * zr)
        if cmd.theta_before is not None:
            expected = min(expected, max(fc - cmd.theta_before, 0.0))
        cmd.theta_after = reading.soil_theta
        cmd.expected_theta_gain = round(expected, 4)
        verification = dict(cmd.verification or {})
        if cmd.theta_before is not None:
            measured = reading.soil_theta - cmd.theta_before
            ratio = measured / expected if expected > 0 else None
            verification.update({"theta_before": cmd.theta_before, "theta_after": reading.soil_theta,
                                 "measured_gain": round(measured, 4), "expected_gain": round(expected, 4),
                                 "response_ratio": round(ratio, 3) if ratio is not None else None})
            if expected > 0.005 and ratio is not None and ratio < cfg.moisture_response_min_ratio:
                raise_anomaly(session, zone_id=zone.id, kind="moisture_response", blocks=False,
                              message=f"{zone.name}: soil moisture rose {measured:.3f} m3/m3, expected about "
                                      f"{expected:.3f}. Check for a leak after the flowmeter, a blocked dripper "
                                      "near the sensor, or sensor placement.")
        cmd.verification = verification
        session.add(cmd)
        done += 1
    session.commit()
    return done


# ---------------------------------------------------------------- identification
def record_identification(session: Session, zone: Zone, data: dict[str, Any],
                          cfg: Settings = default_settings, force_confirmation: bool = False) -> Identification:
    """Store an identification. Confident and consistent AI results are accepted; anything else
    goes to the farmer. A zone whose crop is already confirmed keeps irrigating with that crop
    while the farmer checks a doubtful or disagreeing AI result."""
    ident = Identification(zone_id=zone.id, species_key=data.get("species_key"),
                           species_label=data.get("species_label"), growth_stage=data.get("growth_stage"),
                           confidence=data.get("confidence"), source=data.get("source", "ai"),
                           model_name=data.get("model_name"), image_ref=data.get("image_ref"))
    profile = None
    if ident.species_key:
        profile = session.exec(select(CropProfile).where(CropProfile.key == ident.species_key)).first()
    current = session.get(CropProfile, zone.crop_profile_id) if zone.crop_profile_id else None

    if ident.source == "farmer" and profile:
        ident.status, ident.confirmed_at = "confirmed", utcnow()
        zone.crop_profile_id, zone.identification_status = profile.id, "confirmed"
    elif (profile and not force_confirmation and (ident.confidence or 0) >= cfg.ai_confidence_threshold
          and (current is None or current.id == profile.id)):
        ident.status, ident.confirmed_at = "confirmed", utcnow()
        zone.crop_profile_id, zone.identification_status = profile.id, "confirmed"
    else:
        ident.status = "needs_confirmation"
        label = ident.species_label or ident.species_key or "an unknown plant"
        conf = f"{ident.confidence:.2f}" if ident.confidence is not None else "n/a"
        if current is not None and zone.identification_status == "confirmed":
            why = "is not sure" if (current.key == ident.species_key) else f"suggests {label}"
            raise_anomaly(session, zone_id=zone.id, kind="identification", severity="info", blocks=False,
                          message=f"{zone.name}: the camera {why} (confidence {conf}); the zone is registered as "
                                  f"{current.name}. Confirm or reject the photo. Irrigation continues for "
                                  f"{current.name} meanwhile.")
        else:
            zone.identification_status = "needs_confirmation"
            if profile is None and ident.species_key:
                raise_anomaly(session, zone_id=zone.id, kind="identification", severity="info", blocks=False,
                              message=f"{zone.name}: the camera suggests {label} (confidence {conf}), which has "
                                      "no crop profile yet. Create the profile or choose the crop.")
    session.add(ident)
    session.add(zone)
    session.commit()
    session.refresh(ident)
    return ident


def confirm_identification(session: Session, ident: Identification, species_key: str | None) -> Identification:
    zone = session.get(Zone, ident.zone_id)
    key = species_key or ident.species_key
    profile = session.exec(select(CropProfile).where(CropProfile.key == key)).first() if key else None
    if profile is None:
        raise ValueError(f"No crop profile '{key}'. Create it first.")
    ident.species_key, ident.status, ident.confirmed_at = profile.key, "confirmed", utcnow()
    zone.crop_profile_id, zone.identification_status = profile.id, "confirmed"
    session.add(ident)
    session.add(zone)
    session.commit()
    session.refresh(ident)
    return ident


# ---------------------------------------------------------------- emergency / manual
def set_emergency_stop(session: Session, active: bool) -> SystemState:
    state = system_state(session)
    state.emergency_stop = active
    state.emergency_stop_at = utcnow() if active else None
    if active:
        for cmd in session.exec(select(IrrigationCommand).where(IrrigationCommand.status.in_(ACTIVE_STATUSES))).all():
            if cmd.status == "pending":
                cmd.status, cmd.stop_reason = "cancelled", "emergency_stop"
            else:
                cmd.status = "abort_requested"
            session.add(cmd)
    session.add(state)
    session.commit()
    session.refresh(state)
    return state


def create_manual_command(session: Session, zone: Zone, volume_l: float,
                          cfg: Settings = default_settings) -> IrrigationCommand:
    state = system_state(session)
    if state.emergency_stop:
        raise ValueError("Emergency stop is active.")
    if active_command(session, zone.id):
        raise ValueError("A command is already active for this zone.")
    if volume_l <= 0 or volume_l > cfg.max_volume_per_event_l:
        raise ValueError(f"Volume must be between 0 and {cfg.max_volume_per_event_l} L.")
    reading = latest_zone_reading(session, zone.id)
    duration_min = volume_l / zone.flow_rate_lpm if zone.flow_rate_lpm > 0 else 10
    cmd = IrrigationCommand(zone_id=zone.id, origin="manual", target_volume_l=volume_l,
                            timeout_s=int(max(duration_min * 60 * cfg.timeout_factor, 60)),
                            theta_before=reading.soil_theta if reading and reading.valid else None)
    session.add(cmd)
    session.commit()
    session.refresh(cmd)
    return cmd


# ---------------------------------------------------------------- comparison report
def comparison_report(session: Session, days: int = 14, now: datetime | None = None) -> dict[str, Any]:
    """Measured results per zone. No savings figure is produced unless measurements exist."""
    now = now or utcnow()
    since = now - timedelta(days=days)
    zones_out = []
    for zone in session.exec(select(Zone)).all():
        cmds = session.exec(select(IrrigationCommand).where(IrrigationCommand.zone_id == zone.id,
                                                            IrrigationCommand.created_at >= since,
                                                            IrrigationCommand.delivered_l.is_not(None))).all()
        readings = session.exec(select(SensorReading).where(SensorReading.zone_id == zone.id,
                                                            SensorReading.ts >= since,
                                                            SensorReading.valid == True,  # noqa: E712
                                                            SensorReading.soil_theta.is_not(None))
                                .order_by(SensorReading.ts)).all()
        delivered = [c.delivered_l for c in cmds]
        errors = [abs(c.delivered_l - c.target_volume_l) / c.target_volume_l for c in cmds if c.target_volume_l > 0]
        response = [(c.started_at - c.scheduled_for).total_seconds() for c in cmds
                    if c.started_at and c.scheduled_for and c.started_at >= c.scheduled_for]
        daily: dict[str, float] = {}
        for c in cmds:
            k = c.created_at.date().isoformat()
            daily[k] = daily.get(k, 0.0) + c.delivered_l
        thetas = [r.soil_theta for r in readings]
        profile = session.get(CropProfile, zone.crop_profile_id) if zone.crop_profile_id else None
        zones_out.append({
            "zone_id": zone.id, "zone": zone.name, "strategy": zone.strategy,
            "crop": profile.name if profile else None, "area_m2": zone.area_m2,
            "events": len(cmds),
            "water_delivered_l": {"value": round(sum(delivered), 2), "kind": "measured", "source": "flowmeter"},
            "water_delivered_mm": {"value": round(sum(delivered) / zone.area_m2, 2) if zone.area_m2 else None,
                                   "kind": "calculated", "source": "measured litres / area"},
            "daily_delivered_l": daily,
            "delivery_accuracy_mean_abs_error": {"value": round(mean(errors), 4) if errors else None,
                                                 "kind": "measured", "source": "|delivered - target| / target"},
            "response_time_mean_s": {"value": round(mean(response), 1) if response else None,
                                     "kind": "measured", "source": "valve start - scheduled time"},
            "soil_moisture": {"kind": "measured", "count": len(thetas),
                              "mean": round(mean(thetas), 4) if thetas else None,
                              "min": min(thetas) if thetas else None, "max": max(thetas) if thetas else None,
                              "series": [{"ts": r.ts.isoformat(), "theta": r.soil_theta} for r in readings[-500:]]},
        })

    adaptive = [z for z in zones_out if z["strategy"] == "adaptive" and z["events"]]
    fixed = [z for z in zones_out if z["strategy"] == "fixed" and z["events"]]
    comparison: dict[str, Any] = {"available": False,
                                  "note": "Needs at least one adaptive and one fixed-schedule zone with measured deliveries."}
    if adaptive and fixed:
        a = sum(z["water_delivered_mm"]["value"] for z in adaptive) / len(adaptive)
        f = sum(z["water_delivered_mm"]["value"] for z in fixed) / len(fixed)
        same_crop = {z["crop"] for z in adaptive} == {z["crop"] for z in fixed}
        comparison = {
            "available": True, "period_days": days,
            "adaptive_mm": round(a, 2), "fixed_mm": round(f, 2),
            "measured_difference_pct": round((f - a) / f * 100, 1) if f else None,
            "kind": "measured", "same_crop": same_crop,
            "caveats": [
                "Single trial: repeat over several cycles before claiming a saving.",
                "Compare zones with the same crop and soil; otherwise the difference mixes crop/soil effects."
                if not same_crop else "Same crop in both groups.",
                "Check that crop condition (soil moisture, stress) was comparable - less water is only a saving "
                "if the crop was not stressed.",
            ],
        }
    return {"generated_at": now.isoformat(), "period_days": days, "zones": zones_out, "comparison": comparison}


# ---------------------------------------------------------------- rain now / rain forecast
def raining_now(session: Session, zone_id: int | None, now: datetime,
                cfg: Settings = default_settings) -> tuple[bool, str]:
    """Rain observed right now: rain sensor first (measured), then the camera (AI)."""
    ctrl = latest_controller_reading(session)
    if (ctrl and ctrl.rain_sensor_wet is not None
            and (now - ctrl.ts) <= timedelta(minutes=cfg.reading_max_age_min)):
        if ctrl.rain_sensor_wet:
            return True, "rain sensor"
        return False, ""
    since = now - timedelta(minutes=cfg.rain_observation_max_age_min)
    obs = session.exec(select(VisionObservation).where(VisionObservation.ts >= since,
                                                       VisionObservation.raining_now.is_not(None))
                       .order_by(VisionObservation.ts.desc())).all()
    obs = [o for o in obs if o.zone_id in (zone_id, None)]
    if obs and obs[0].raining_now and (obs[0].rain_confidence or 0) >= cfg.rain_visual_min_confidence:
        return True, f"camera, confidence {obs[0].rain_confidence:.2f}"
    return False, ""


def rain_forecast_alert(session: Session, now: datetime | None = None,
                        cfg: Settings = default_settings) -> Anomaly | None:
    """Notify the farmer when tomorrow's forecast shows likely rain; clear the notice when it no longer does."""
    now = now or utcnow()
    tomorrow = local_today(now, cfg) + timedelta(days=1)
    wd = session.exec(select(WeatherDay).where(WeatherDay.day == tomorrow)).first()
    likely = (wd is not None and (wd.precip_probability_pct or 0) >= cfg.rain_postpone_probability_pct
              and (wd.precip_mm or 0) >= cfg.rain_alert_min_mm)
    open_alerts = session.exec(select(Anomaly).where(Anomaly.kind == "rain_forecast",
                                                     Anomaly.acknowledged == False)).all()  # noqa: E712
    if not likely:
        for a in open_alerts:
            a.acknowledged, a.acknowledged_at = True, now
            session.add(a)
        session.commit()
        return None
    msg = (f"Rain expected on {tomorrow:%d/%m}: {wd.precip_mm:.1f} mm, {wd.precip_probability_pct:.0f}% probability "
           "(Open-Meteo forecast). Irrigation will be postponed if the rain covers the need.")
    for a in open_alerts:
        if a.message != msg:
            a.acknowledged, a.acknowledged_at = True, now
            session.add(a)
    session.commit()
    return raise_anomaly(session, zone_id=None, kind="rain_forecast", severity="info", blocks=False, message=msg)


# ---------------------------------------------------------------- vision observations
PEST_LABELS = {
    "leaf_miner": "leaf-miner damage (possible Tuta absoluta)",
    "insects": "insects on the leaves (aphids / whiteflies?)",
    "yellowing": "yellowing leaves",
    "leaf_spots": "leaf spots or blight symptoms",
}


def ingest_vision(session: Session, payload: dict[str, Any], now: datetime | None = None,
                  cfg: Settings = default_settings) -> dict[str, Any]:
    """Store one vision-service result, feed crop identification, raise crop-health alerts."""
    now = now or utcnow()
    zone = session.get(Zone, payload["zone_id"]) if payload.get("zone_id") else None
    crop = payload.get("crop") or {}
    stage = payload.get("stage") or {}
    rain = payload.get("rain") or {}
    trap = payload.get("trap") or {}
    obs = VisionObservation(
        ts=now, zone_id=zone.id if zone else None, plant_ref=payload.get("plant_ref"),
        source=payload.get("source", "mast"), image_ref=payload.get("image_ref"),
        canopy_cover_pct=(payload.get("canopy") or {}).get("canopy_cover_pct"),
        crop_key=crop.get("key"), crop_confidence=crop.get("confidence"),
        growth_stage=stage.get("key"), stage_confidence=stage.get("confidence"),
        raining_now=rain.get("raining_now"), rain_confidence=rain.get("confidence"),
        trap_insect_count=trap.get("insect_count"), engines=payload.get("engines") or {}, payload=payload,
    )
    session.add(obs)
    session.commit()
    session.refresh(obs)
    plant = growth.find_or_create_plant(session, zone, obs.plant_ref) if (zone and obs.plant_ref) else None
    stage_obs = None
    if zone and stage.get("key") and crop.get("key") not in (None, "other", "unknown"):
        stage_obs = growth.record_ai_stage(session, zone, crop["key"], stage["key"], stage.get("confidence"),
                                           plant=plant, visual_cues=stage.get("visual_cues"),
                                           image_ref=obs.image_ref, vision_observation_id=obs.id,
                                           today=local_today(now, cfg))
    where = (zone.name if zone else "Farm") + (f", plant {obs.plant_ref}" if obs.plant_ref else "")
    already_open = {a.id for a in open_anomalies(session, obs.zone_id)}
    alerts: list[Anomaly] = []
    ident = None

    if zone and crop.get("key") and crop.get("key") != "other":
        unsure = crop["key"] == "unknown"           # the vision service abstained: ask the farmer
        ident = record_identification(session, zone, {
            "species_key": crop.get("best_guess") if unsure else crop["key"],
            "species_label": crop.get("label"), "growth_stage": stage.get("key"),
            "confidence": crop.get("confidence"), "source": "ai",
            "model_name": (payload.get("engines") or {}).get("crop"), "image_ref": obs.image_ref},
            cfg, force_confirmation=unsure)

    for p in payload.get("pests") or []:
        if p.get("label") in (None, "healthy") or (p.get("confidence") or 0) < cfg.pest_alert_confidence:
            continue
        if "zero-shot" in str(p.get("engine", "")):
            continue                     # untrained screening: never an alert (older vision service versions)
        name = PEST_LABELS.get(p["label"], p["label"])
        alerts.append(raise_anomaly(session, zone_id=obs.zone_id, kind=f"pest:{p['label']}", blocks=False,
                                    message=f"{where}: {name} detected (confidence {p['confidence']:.2f}, "
                                            f"{p.get('engine', 'AI')}). Check the plants and confirm."))
    for a in payload.get("animals") or []:
        if (a.get("confidence") or 0) < cfg.animal_alert_confidence:
            continue
        alerts.append(raise_anomaly(session, zone_id=obs.zone_id, kind=f"animal:{a['label']}", severity="warning",
                                    blocks=False, message=f"{where}: {a['label']} seen in the field "
                                                          f"(confidence {a['confidence']:.2f})."))
    motion = payload.get("motion") or {}
    if motion.get("intrusion") and not payload.get("animals"):
        alerts.append(raise_anomaly(session, zone_id=obs.zone_id, kind="motion", blocks=False,
                                    message=f"{where}: unexplained movement in the field "
                                            f"({motion.get('changed_pct', 0):.1f}% of the image changed)."))
    if trap.get("trap_found") and trap.get("insect_count") is not None:
        prev = session.exec(select(VisionObservation).where(VisionObservation.source == "trap",
                                                            VisionObservation.zone_id == obs.zone_id,
                                                            VisionObservation.id != obs.id,
                                                            VisionObservation.trap_insect_count.is_not(None))
                            .order_by(VisionObservation.ts.desc())).first()
        new = trap["insect_count"] - (prev.trap_insect_count if prev else 0)
        if trap.get("trap_replaced"):
            new = trap["insect_count"]
        if new >= cfg.trap_alert_new_insects:
            alerts.append(raise_anomaly(session, zone_id=obs.zone_id, kind="insect_trap", blocks=False,
                                        message=f"{where}: {new} new insects on the sticky trap since the last photo "
                                                f"({trap['insect_count']} in total)."))
    stage_view = growth.assess(session, zone, plant, today=local_today(now, cfg)) if zone and zone.planting_date else None
    irrigation = None
    if zone is not None and zone.crop_profile_id and crop.get("key") not in ("other", "unknown"):
        from .irrigation_plan import build_plan, plan_summary
        profile = session.get(CropProfile, zone.crop_profile_id)
        if profile and crop.get("key") in (None, profile.key):
            irrigation = plan_summary(build_plan(session, zone, now, cfg))
            irrigation["for_crop"] = profile.key
        elif profile:
            irrigation = {"status": "insufficient_data",
                          "missing": [f"the photo shows {crop.get('key')}, the zone is registered as {profile.key}: "
                                      "confirm the crop before an irrigation plan is given"]}
    return {"observation_id": obs.id,
            "identification": {"id": ident.id, "status": ident.status} if ident else None,
            "stage_evidence_id": stage_obs.id if stage_obs else None,
            "stage": stage_view,
            "irrigation": irrigation,
            "health_hint": payload.get("health"),
            # new = raised by this photo; False = the same alert was already open (not raised again)
            "alerts": [{"id": a.id, "kind": a.kind, "message": a.message, "new": a.id not in already_open}
                       for a in alerts]}
