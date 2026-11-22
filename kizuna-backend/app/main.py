"""KIZUNA - AI-powered dynamic irrigation: backend API.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
Docs: http://localhost:8000/docs
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from . import growth, services
from .agronomy.crop_cycles import CYCLES, cycle_to_dict
from .agronomy.reference_data import CROPS, IRRIGATION_EFFICIENCY, SOILS, STAGE_ALTERNATIVES
from .config import settings
from .db import engine, get_session, init_db
from .models import (Anomaly, CropProfile, Decision, Identification, IrrigationCommand, Plant, SensorReading,
                     StageObservation, VisionObservation, WeatherDay, Zone, utcnow)
from .climate import climate_needs_refresh, load_temperatures, refresh_climate
from .agronomy.thermal import gdd as _gdd
from .models import ClimateDay
from .weather import refresh_forecast
from . import dashboard_api

log = logging.getLogger("kizuna")


# ---------------------------------------------------------------- background scheduler
async def scheduler_loop() -> None:
    last_weather = datetime.min
    last_decision = datetime.min
    while True:
        try:
            now = utcnow()
            with Session(engine) as session:
                if now - last_weather >= timedelta(minutes=settings.weather_interval_min):
                    await asyncio.to_thread(refresh_forecast, session)
                    services.rain_forecast_alert(session, now)
                    if climate_needs_refresh(session, growth.farm_today()):
                        await asyncio.to_thread(refresh_climate, session)   # normals for degree-days
                    last_weather = now
                services.run_fixed_schedules(session, now)
                services.check_moisture_responses(session, now)
                if now - last_decision >= timedelta(minutes=settings.decision_interval_min):
                    services.run_all_decisions(session, now)
                    last_decision = now
        except Exception:  # keep the loop alive; errors are logged
            log.exception("scheduler cycle failed")
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    task = asyncio.create_task(scheduler_loop()) if settings.scheduler_enabled else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="KIZUNA Irrigation API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def device_auth(x_device_key: str | None = Header(default=None)) -> None:
    if settings.device_api_key and x_device_key != settings.device_api_key:
        raise HTTPException(status_code=401, detail="invalid device key")


def _get(session: Session, model, obj_id: int):
    obj = session.get(model, obj_id)
    if obj is None:
        raise HTTPException(404, f"{model.__name__} {obj_id} not found")
    return obj


# ---------------------------------------------------------------- request bodies
class ZoneReadingIn(BaseModel):
    zone_id: Optional[int] = None
    valve_channel: Optional[int] = None
    soil_raw: Optional[float] = None
    soil_theta: Optional[float] = None
    soil_temp_c: Optional[float] = None
    soil_ec_ds_m: Optional[float] = None


class ControllerReadingIn(BaseModel):
    air_temp_c: Optional[float] = None
    air_rh_pct: Optional[float] = None
    tank_level_pct: Optional[float] = None
    flow_lpm: Optional[float] = None
    valves_open: list[int] = Field(default_factory=list)
    rain_sensor_wet: Optional[bool] = None


class ReadingsIn(BaseModel):
    controller: Optional[ControllerReadingIn] = None
    zones: list[ZoneReadingIn] = Field(default_factory=list)


class CommandReportIn(BaseModel):
    delivered_l: float
    stop_reason: str = "volume_reached"   # volume_reached | timeout | manual_stop | emergency_stop | error


class CropIn(BaseModel):
    key: str
    name: str
    kc_ini: float
    kc_mid: float
    kc_end: float
    max_height_m: float
    l_ini: int
    l_dev: int
    l_mid: int
    l_late: int
    zr_ini_m: float
    zr_max_m: float
    p: float
    season_label: str = ""
    source: str = "user-defined"
    notes: list[str] = Field(default_factory=list)


class ZoneIn(BaseModel):
    name: str
    valve_channel: int
    crop_key: Optional[str] = None
    planting_date: Optional[date] = None
    soil_type: str = "loam"
    theta_fc: Optional[float] = None
    theta_wp: Optional[float] = None
    root_depth_override_m: Optional[float] = None
    area_m2: float = 10.0
    plant_count: Optional[int] = None
    irrigation_method: str = "drip"
    efficiency: Optional[float] = None
    flow_rate_lpm: float = 10.0
    refill_fraction: float = Field(1.0, gt=0, le=1.0)
    strategy: str = "adaptive"
    fixed_volume_l: float = 0.0
    fixed_time: str = "07:00"
    sensor_raw_dry: Optional[float] = None
    sensor_theta_dry: Optional[float] = None
    sensor_raw_wet: Optional[float] = None
    sensor_theta_wet: Optional[float] = None
    enabled: bool = True


class IdentificationIn(BaseModel):
    species_key: Optional[str] = None
    species_label: Optional[str] = None
    growth_stage: Optional[str] = None
    confidence: Optional[float] = Field(None, ge=0, le=1)
    source: str = "ai"                   # ai | farmer
    model_name: Optional[str] = None
    image_ref: Optional[str] = None


class ConfirmIn(BaseModel):
    species_key: Optional[str] = None


class ManualIrrigationIn(BaseModel):
    volume_l: float


class EmergencyIn(BaseModel):
    active: bool


class ModeIn(BaseModel):
    mode: str  # auto | manual


# ---------------------------------------------------------------- general
app.include_router(dashboard_api.router)


@app.get("/", include_in_schema=False)
def root() -> dict[str, Any]:
    """The dashboard lives in its own folder (kizuna-dashboard, port 8080); this is the API."""
    return {"service": "AGRIWISE backend (API)", "api_docs": "/docs",
            "dashboard": "run kizuna-dashboard: python server.py  ->  http://localhost:8080"}


@app.get("/health")
def health(session: Session = Depends(get_session)) -> dict[str, Any]:
    state = services.system_state(session)
    return {"status": "ok", "time_utc": utcnow().isoformat(), "mode": state.mode,
            "emergency_stop": state.emergency_stop,
            "controller_last_seen": state.controller_last_seen.isoformat() if state.controller_last_seen else None}


@app.get("/api/reference")
def reference() -> dict[str, Any]:
    """FAO-56 reference data used as defaults."""
    return {"crops": {k: v.__dict__ for k, v in CROPS.items()}, "stage_alternatives": STAGE_ALTERNATIVES,
            "soils": {k: v.__dict__ for k, v in SOILS.items()}, "irrigation_efficiency": IRRIGATION_EFFICIENCY}


# ---------------------------------------------------------------- crops
@app.get("/api/crops")
def list_crops(session: Session = Depends(get_session)) -> list[CropProfile]:
    return list(session.exec(select(CropProfile)).all())


@app.post("/api/crops", status_code=201)
def create_crop(body: CropIn, session: Session = Depends(get_session)) -> CropProfile:
    if session.exec(select(CropProfile).where(CropProfile.key == body.key)).first():
        raise HTTPException(409, "crop key already exists")
    crop = CropProfile(**body.model_dump())
    session.add(crop)
    session.commit()
    session.refresh(crop)
    return crop


@app.put("/api/crops/{crop_id}")
def update_crop(crop_id: int, body: CropIn, session: Session = Depends(get_session)) -> CropProfile:
    crop = _get(session, CropProfile, crop_id)
    for k, v in body.model_dump().items():
        setattr(crop, k, v)
    crop.updated_at = utcnow()
    session.add(crop)
    session.commit()
    session.refresh(crop)
    return crop


# ---------------------------------------------------------------- zones
def _apply_zone(zone: Zone, body: ZoneIn, session: Session) -> None:
    data = body.model_dump(exclude={"crop_key"})
    if body.soil_type not in SOILS:
        raise HTTPException(422, f"unknown soil_type, use one of {list(SOILS)}")
    if body.strategy not in ("adaptive", "fixed"):
        raise HTTPException(422, "strategy must be 'adaptive' or 'fixed'")
    for k, v in data.items():
        setattr(zone, k, v)
    if body.crop_key:
        crop = session.exec(select(CropProfile).where(CropProfile.key == body.crop_key)).first()
        if crop is None:
            raise HTTPException(422, f"unknown crop_key {body.crop_key}")
        if zone.crop_profile_id != crop.id:
            zone.crop_profile_id = crop.id
            zone.identification_status = "confirmed"  # chosen by the farmer


@app.get("/api/zones")
def list_zones(session: Session = Depends(get_session)) -> list[Zone]:
    return list(session.exec(select(Zone)).all())


@app.post("/api/zones", status_code=201)
def create_zone(body: ZoneIn, session: Session = Depends(get_session)) -> Zone:
    zone = Zone(name=body.name, valve_channel=body.valve_channel)
    _apply_zone(zone, body, session)
    session.add(zone)
    session.commit()
    session.refresh(zone)
    return zone


@app.put("/api/zones/{zone_id}")
def update_zone(zone_id: int, body: ZoneIn, session: Session = Depends(get_session)) -> Zone:
    zone = _get(session, Zone, zone_id)
    _apply_zone(zone, body, session)
    session.add(zone)
    session.commit()
    session.refresh(zone)
    return zone


@app.get("/api/zones/{zone_id}/status")
def zone_status(zone_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    zone = _get(session, Zone, zone_id)
    crop = session.get(CropProfile, zone.crop_profile_id) if zone.crop_profile_id else None
    decision = session.exec(select(Decision).where(Decision.zone_id == zone_id).order_by(Decision.ts.desc())).first()
    ident = session.exec(select(Identification).where(Identification.zone_id == zone_id)
                         .order_by(Identification.ts.desc())).first()
    return {
        "zone": zone, "crop": crop,
        "latest_reading": services.latest_zone_reading(session, zone_id),
        "latest_decision": decision,
        "latest_identification": ident,
        "active_command": services.active_command(session, zone_id),
        "open_anomalies": services.open_anomalies(session, zone_id),
    }


@app.get("/api/zones/{zone_id}/readings")
def zone_readings(zone_id: int, hours: int = 48, session: Session = Depends(get_session)) -> list[SensorReading]:
    since = utcnow() - timedelta(hours=hours)
    return list(session.exec(select(SensorReading).where(SensorReading.zone_id == zone_id, SensorReading.ts >= since)
                             .order_by(SensorReading.ts)).all())


@app.post("/api/zones/{zone_id}/decide")
def decide_zone(zone_id: int, session: Session = Depends(get_session)) -> Decision:
    zone = _get(session, Zone, zone_id)
    return services.run_decision(session, zone)


@app.post("/api/decide-all")
def decide_all(session: Session = Depends(get_session)) -> list[Decision]:
    return services.run_all_decisions(session)


@app.get("/api/zones/{zone_id}/decisions")
def zone_decisions(zone_id: int, limit: int = 50, session: Session = Depends(get_session)) -> list[Decision]:
    return list(session.exec(select(Decision).where(Decision.zone_id == zone_id)
                             .order_by(Decision.ts.desc()).limit(limit)).all())


@app.post("/api/zones/{zone_id}/manual-irrigation", status_code=201)
def manual_irrigation(zone_id: int, body: ManualIrrigationIn, session: Session = Depends(get_session)) -> IrrigationCommand:
    zone = _get(session, Zone, zone_id)
    try:
        return services.create_manual_command(session, zone, body.volume_l)
    except ValueError as exc:
        raise HTTPException(409, str(exc))


# ---------------------------------------------------------------- identification (vision module)
@app.post("/api/zones/{zone_id}/identifications", status_code=201)
def post_identification(zone_id: int, body: IdentificationIn, session: Session = Depends(get_session)) -> Identification:
    zone = _get(session, Zone, zone_id)
    return services.record_identification(session, zone, body.model_dump())


@app.post("/api/identifications/{ident_id}/confirm")
def confirm(ident_id: int, body: ConfirmIn, session: Session = Depends(get_session)) -> Identification:
    ident = _get(session, Identification, ident_id)
    try:
        return services.confirm_identification(session, ident, body.species_key)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@app.post("/api/identifications/{ident_id}/reject")
def reject(ident_id: int, session: Session = Depends(get_session)) -> Identification:
    ident = _get(session, Identification, ident_id)
    ident.status = "rejected"
    session.add(ident)
    session.commit()
    session.refresh(ident)
    return ident


# ---------------------------------------------------------------- commands & system
@app.get("/api/commands")
def list_commands(limit: int = 50, session: Session = Depends(get_session)) -> list[IrrigationCommand]:
    return list(session.exec(select(IrrigationCommand).order_by(IrrigationCommand.created_at.desc()).limit(limit)).all())


@app.post("/api/commands/{cmd_id}/stop")
def stop_command(cmd_id: int, session: Session = Depends(get_session)) -> IrrigationCommand:
    cmd = _get(session, IrrigationCommand, cmd_id)
    if cmd.status == "pending":
        cmd.status, cmd.stop_reason = "cancelled", "manual_stop"
    elif cmd.status in ("sent", "running"):
        cmd.status = "abort_requested"
    session.add(cmd)
    session.commit()
    session.refresh(cmd)
    return cmd


@app.post("/api/system/emergency-stop")
def emergency_stop(body: EmergencyIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    state = services.set_emergency_stop(session, body.active)
    return {"emergency_stop": state.emergency_stop, "at": state.emergency_stop_at}


@app.post("/api/system/mode")
def set_mode(body: ModeIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    if body.mode not in ("auto", "manual"):
        raise HTTPException(422, "mode must be 'auto' or 'manual'")
    state = services.system_state(session)
    state.mode = body.mode
    session.add(state)
    session.commit()
    return {"mode": state.mode}


@app.get("/api/anomalies")
def list_anomalies(include_acknowledged: bool = False, session: Session = Depends(get_session)) -> list[Anomaly]:
    q = select(Anomaly).order_by(Anomaly.ts.desc())
    if not include_acknowledged:
        q = q.where(Anomaly.acknowledged == False)  # noqa: E712
    return list(session.exec(q).all())


@app.post("/api/anomalies/{anomaly_id}/ack")
def ack_anomaly(anomaly_id: int, session: Session = Depends(get_session)) -> Anomaly:
    a = _get(session, Anomaly, anomaly_id)
    a.acknowledged, a.acknowledged_at = True, utcnow()
    session.add(a)
    session.commit()
    session.refresh(a)
    return a


# ---------------------------------------------------------------- weather
@app.post("/api/weather/refresh")
def weather_refresh(session: Session = Depends(get_session)) -> dict[str, Any]:
    n = refresh_forecast(session)
    alert = services.rain_forecast_alert(session)
    return {"days_stored": n, "online": n > 0, "rain_alert": alert.message if alert else None}


@app.post("/api/climate/refresh")
def climate_refresh(years: int = 5, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Download the daily temperatures of the last years (climate normals for degree-days)."""
    from . import climate as climate_module
    n = refresh_climate(session, years=max(3, min(years, 15)))
    return {"days_stored": n, "online": n > 0, "error": None if n > 0 else climate_module.LAST_ERROR}


@app.get("/api/climate/status")
def climate_status(session: Session = Depends(get_session)) -> dict[str, Any]:
    days = session.exec(select(ClimateDay.day)).all()
    years = sorted({d.year for d in days})
    return {"days": len(days), "years": years, "first": min(days).isoformat() if days else None,
            "last": max(days).isoformat() if days else None,
            "normals_available": len(years) >= 3,
            "source": "Open-Meteo historical archive (ERA5 reanalysis)"}


@app.get("/api/weather")
def weather(session: Session = Depends(get_session)) -> list[WeatherDay]:
    return list(session.exec(select(WeatherDay).order_by(WeatherDay.day.desc()).limit(10)).all())


# ---------------------------------------------------------------- reports
@app.get("/api/report/comparison")
def report(days: int = 14, session: Session = Depends(get_session)) -> dict[str, Any]:
    return services.comparison_report(session, days)


# ---------------------------------------------------------------- ESP32 controller
@app.post("/api/controller/readings", dependencies=[Depends(device_auth)])
def controller_readings(body: ReadingsIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    return services.ingest_readings(session, body.model_dump())


@app.get("/api/controller/next-command", dependencies=[Depends(device_auth)])
def controller_next(session: Session = Depends(get_session)) -> dict[str, Any]:
    return services.next_controller_command(session)


@app.post("/api/controller/commands/{cmd_id}/started", dependencies=[Depends(device_auth)])
def controller_started(cmd_id: int, session: Session = Depends(get_session)) -> IrrigationCommand:
    return services.mark_started(session, _get(session, IrrigationCommand, cmd_id))


@app.post("/api/controller/commands/{cmd_id}/report", dependencies=[Depends(device_auth)])
def controller_report(cmd_id: int, body: CommandReportIn, session: Session = Depends(get_session)) -> IrrigationCommand:
    cmd = _get(session, IrrigationCommand, cmd_id)
    return services.report_finished(session, cmd, body.delivered_l, body.stop_reason)


# ---------------------------------------------------------------- vision service
class VisionIn(BaseModel):
    zone_id: Optional[int] = None
    plant_ref: Optional[str] = None
    source: str = "mast"
    image_ref: Optional[str] = None
    canopy: Optional[dict[str, Any]] = None
    crop: Optional[dict[str, Any]] = None
    stage: Optional[dict[str, Any]] = None
    pests: list[dict[str, Any]] = Field(default_factory=list)
    animals: list[dict[str, Any]] = Field(default_factory=list)
    motion: Optional[dict[str, Any]] = None
    rain: Optional[dict[str, Any]] = None
    trap: Optional[dict[str, Any]] = None
    health: Optional[dict[str, Any]] = None      # untrained zero-shot hint: shown, never an alert
    engines: dict[str, Any] = Field(default_factory=dict)


@app.post("/api/vision/observations", dependencies=[Depends(device_auth)], status_code=201)
def vision_observation(body: VisionIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    if body.zone_id is not None:
        _get(session, Zone, body.zone_id)
    return services.ingest_vision(session, body.model_dump())


@app.get("/api/vision/observations")
def list_vision(zone_id: Optional[int] = None, limit: int = 50,
                session: Session = Depends(get_session)) -> list[VisionObservation]:
    q = select(VisionObservation).order_by(VisionObservation.ts.desc()).limit(limit)
    if zone_id is not None:
        q = q.where(VisionObservation.zone_id == zone_id)
    return list(session.exec(q).all())


# ---------------------------------------------------------------- growth cycles, stages, plants (step 1)
class StageConfirmIn(BaseModel):
    stage_key: str
    plant_id: Optional[int] = None
    observed_on: Optional[date] = None
    note: Optional[str] = None


class ObservationConfirmIn(BaseModel):
    stage_key: Optional[str] = None       # required when the photo was compatible with several stages
    note: Optional[str] = None


class PlantIn(BaseModel):
    label: str
    crop_key: Optional[str] = None        # None = same crop as the zone
    planting_date: Optional[date] = None  # None = zone's planting date
    has_own_valve: bool = False
    valve_channel: Optional[int] = None


def _plant(session: Session, plant_id: int) -> Plant:
    return _get(session, Plant, plant_id)


@app.get("/api/crops/{crop_key}/cycle")
def crop_cycle(crop_key: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    profile = session.exec(select(CropProfile).where(CropProfile.key == crop_key)).first()
    if profile is None:
        raise HTTPException(404, f"crop {crop_key} not found")
    return cycle_to_dict(growth.cycle_of(profile))


@app.get("/api/cycles")
def all_cycles() -> dict[str, Any]:
    return {k: cycle_to_dict(c) for k, c in CYCLES.items()}


@app.get("/api/zones/{zone_id}/stage")
def zone_stage(zone_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    return growth.assess(session, _get(session, Zone, zone_id))


@app.get("/api/zones/{zone_id}/thermal")
def zone_thermal(zone_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Daily degree-days of the season vs the climate normal, with the source of every day."""
    zone = _get(session, Zone, zone_id)
    profile = session.get(CropProfile, zone.crop_profile_id) if zone.crop_profile_id else None
    if profile is None or zone.planting_date is None:
        raise HTTPException(409, "zone has no crop or planting date")
    cycle = growth.cycle_of(profile)
    if cycle.thermal is None:
        return {"available": False, "reason": "No documented degree-day thresholds for this crop yet."}
    today = growth.farm_today()
    temps = load_temperatures(session, zone.planting_date, today)
    rows, acc_a, acc_n = [], 0.0, 0.0
    d = zone.planting_date
    while d < today:
        t = temps.actual(d)
        n = temps.normal_gdd(d, cycle.thermal)
        a = _gdd(t.tmin, t.tmax, cycle.thermal) if t else None
        acc_a += a if a is not None else (n or 0.0)
        acc_n += n or 0.0
        rows.append({"day": d.isoformat(), "tmin": t.tmin if t else None, "tmax": t.tmax if t else None,
                     "kind": t.kind if t else "missing", "gdd": round(a, 2) if a is not None else None,
                     "normal_gdd": round(n, 2) if n is not None else None,
                     "cum_gdd": round(acc_a, 1), "cum_normal_gdd": round(acc_n, 1)})
        d += timedelta(days=1)
    return {"available": True, "crop_key": profile.key, "t_base_c": cycle.thermal.t_base_c,
            "t_upper_c": cycle.thermal.t_upper_c, "source": cycle.thermal.source, "days": rows}


@app.get("/api/zones/{zone_id}/irrigation-plan")
def zone_irrigation_plan(zone_id: int, days: int = 7, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Frequency, irrigations per week, volume, duration, next irrigation and weekly water (FAO-56 projection)."""
    from .irrigation_plan import build_plan
    return build_plan(session, _get(session, Zone, zone_id), horizon_days=max(3, min(days, 14)))


@app.get("/api/zones/{zone_id}/stage/history")
def zone_stage_history(zone_id: int, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return growth.history(session, _get(session, Zone, zone_id))


@app.post("/api/zones/{zone_id}/stage/confirm", status_code=201)
def zone_stage_confirm(zone_id: int, body: StageConfirmIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    zone = _get(session, Zone, zone_id)
    plant = _plant(session, body.plant_id) if body.plant_id else None
    if plant and plant.zone_id != zone.id:
        raise HTTPException(422, "plant not in this zone")
    profile, _ = growth.crop_and_planting(session, zone, plant)
    if profile is None:
        raise HTTPException(422, "no crop registered for this zone/plant")
    try:
        growth.confirm_stage(session, zone, profile.key, body.stage_key, plant, body.observed_on, body.note)
    except (KeyError, ValueError) as exc:
        raise HTTPException(422, str(exc).strip("'"))
    return growth.assess(session, zone, plant)


@app.post("/api/stage-observations/{obs_id}/confirm")
def stage_observation_confirm(obs_id: int, body: ObservationConfirmIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    o = _get(session, StageObservation, obs_id)
    zone = _get(session, Zone, o.zone_id)
    plant = _plant(session, o.plant_id) if o.plant_id else None
    key = body.stage_key or o.stage_key or (o.candidate_stages[0] if len(o.candidate_stages) == 1 else None)
    if key is None:
        raise HTTPException(422, f"the photo fits several stages {o.candidate_stages}: choose stage_key")
    try:
        growth.confirm_stage(session, zone, o.crop_key, key, plant, note=body.note, from_observation=o)
    except (KeyError, ValueError) as exc:
        raise HTTPException(422, str(exc).strip("'"))
    return growth.assess(session, zone, plant)


@app.post("/api/stage-observations/{obs_id}/reject")
def stage_observation_reject(obs_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    o = _get(session, StageObservation, obs_id)
    o.status = "rejected"
    session.add(o)
    session.commit()
    zone = _get(session, Zone, o.zone_id)
    return growth.assess(session, zone, _plant(session, o.plant_id) if o.plant_id else None)


@app.get("/api/zones/{zone_id}/plants")
def zone_plants(zone_id: int, session: Session = Depends(get_session)) -> list[Plant]:
    _get(session, Zone, zone_id)
    return list(session.exec(select(Plant).where(Plant.zone_id == zone_id)).all())


@app.post("/api/zones/{zone_id}/plants", status_code=201)
def create_plant(zone_id: int, body: PlantIn, session: Session = Depends(get_session)) -> Plant:
    _get(session, Zone, zone_id)
    if session.exec(select(Plant).where(Plant.zone_id == zone_id, Plant.label == body.label)).first():
        raise HTTPException(409, "a plant with this label already exists in the zone")
    crop_id = None
    if body.crop_key:
        crop = session.exec(select(CropProfile).where(CropProfile.key == body.crop_key)).first()
        if crop is None:
            raise HTTPException(422, f"unknown crop_key {body.crop_key}")
        crop_id = crop.id
    p = Plant(zone_id=zone_id, label=body.label, crop_profile_id=crop_id, planting_date=body.planting_date,
              has_own_valve=body.has_own_valve, valve_channel=body.valve_channel)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


@app.get("/api/plants/{plant_id}/stage")
def plant_stage(plant_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    p = _plant(session, plant_id)
    return growth.assess(session, _get(session, Zone, p.zone_id), p)


@app.get("/api/plants/{plant_id}/stage/history")
def plant_stage_history(plant_id: int, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    p = _plant(session, plant_id)
    return growth.history(session, _get(session, Zone, p.zone_id), p)
