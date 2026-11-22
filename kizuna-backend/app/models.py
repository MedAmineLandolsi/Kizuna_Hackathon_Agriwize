"""Database tables (SQLModel / SQLite)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from pydantic import NaiveDatetime
from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite stores no timezone)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CropProfile(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)
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
    source: str = ""
    notes: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    updated_at: NaiveDatetime = Field(default_factory=utcnow)


class Zone(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    valve_channel: int                       # relay/valve index on the ESP32 (0, 1, ...)
    crop_profile_id: Optional[int] = Field(default=None, foreign_key="cropprofile.id")
    planting_date: Optional[date] = None
    soil_type: str = "loam"
    theta_fc: Optional[float] = None         # override Table 19 value with a field measurement
    theta_wp: Optional[float] = None
    root_depth_override_m: Optional[float] = None
    area_m2: float = 10.0
    plant_count: Optional[int] = None
    irrigation_method: str = "drip"
    efficiency: Optional[float] = None       # override typical efficiency
    flow_rate_lpm: float = 10.0              # nominal flow with the valve open (for duration/timeout)
    refill_fraction: float = 1.0             # 1.0 = refill to field capacity
    strategy: str = "adaptive"               # "adaptive" | "fixed" (control zone for the comparison)
    fixed_volume_l: float = 0.0
    fixed_time: str = "07:00"                # local time, HH:MM
    # Soil sensor calibration: two (raw ADC, volumetric water content) points
    sensor_raw_dry: Optional[float] = None
    sensor_theta_dry: Optional[float] = None
    sensor_raw_wet: Optional[float] = None
    sensor_theta_wet: Optional[float] = None
    identification_status: str = "unconfirmed"  # unconfirmed | needs_confirmation | confirmed
    enabled: bool = True


class SensorReading(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ts: NaiveDatetime = Field(default_factory=utcnow, index=True)
    zone_id: Optional[int] = Field(default=None, foreign_key="zone.id", index=True)  # None = controller-wide
    soil_raw: Optional[float] = None
    soil_theta: Optional[float] = None       # m3/m3 (measured or converted from raw)
    soil_temp_c: Optional[float] = None
    soil_ec_ds_m: Optional[float] = None
    air_temp_c: Optional[float] = None
    air_rh_pct: Optional[float] = None
    tank_level_pct: Optional[float] = None
    flow_lpm: Optional[float] = None
    valves_open: Optional[list[int]] = Field(default=None, sa_column=Column(JSON))
    rain_sensor_wet: Optional[bool] = None   # optional rain sensor on the controller
    valid: bool = True
    issues: list[str] = Field(default_factory=list, sa_column=Column(JSON))


class WeatherDay(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    day: date = Field(index=True)
    et0_mm: Optional[float] = None
    precip_mm: Optional[float] = None
    precip_probability_pct: Optional[float] = None
    tmax_c: Optional[float] = None
    tmin_c: Optional[float] = None
    rh_min_pct: Optional[float] = None
    u2_ms: Optional[float] = None
    source: str = "open-meteo"
    fetched_at: NaiveDatetime = Field(default_factory=utcnow)


class DashboardLayout(SQLModel, table=True):
    """Positions of the elements on the farm map (shared by every screen that opens the dashboard)."""
    id: Optional[int] = Field(default=1, primary_key=True)
    data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    updated_at: NaiveDatetime = Field(default_factory=utcnow)


class ClimateDay(SQLModel, table=True):
    """Daily air temperature of past days at the farm (Open-Meteo historical archive, ERA5 reanalysis).

    Used for (1) the climate normals of the farm (mean of the last years) and (2) the actual
    temperatures of the current season when no on-farm sensor or forecast value exists.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    day: date = Field(index=True, unique=True)
    tmax_c: Optional[float] = None
    tmin_c: Optional[float] = None
    source: str = "open-meteo-archive"
    fetched_at: NaiveDatetime = Field(default_factory=utcnow)


class IrrigationCommand(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    zone_id: int = Field(foreign_key="zone.id", index=True)
    decision_id: Optional[int] = Field(default=None, foreign_key="decision.id")
    origin: str = "auto"                     # auto | fixed | manual
    created_at: NaiveDatetime = Field(default_factory=utcnow)
    scheduled_for: NaiveDatetime = Field(default_factory=utcnow)
    target_volume_l: float
    timeout_s: int
    status: str = "pending"                  # pending | sent | running | done | aborted | cancelled
    sent_at: Optional[NaiveDatetime] = None
    started_at: Optional[NaiveDatetime] = None
    finished_at: Optional[NaiveDatetime] = None
    delivered_l: Optional[float] = None
    stop_reason: Optional[str] = None        # volume_reached | timeout | manual_stop | emergency_stop | ...
    theta_before: Optional[float] = None
    theta_after: Optional[float] = None
    expected_theta_gain: Optional[float] = None
    verification: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class Decision(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    zone_id: int = Field(foreign_key="zone.id", index=True)
    ts: NaiveDatetime = Field(default_factory=utcnow, index=True)
    action: str                              # irrigate_now | schedule | no_irrigation | postpone_rain | blocked
    reasons: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    values: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    target_volume_l: Optional[float] = None
    scheduled_for: Optional[NaiveDatetime] = None
    command_id: Optional[int] = None


class Anomaly(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ts: NaiveDatetime = Field(default_factory=utcnow, index=True)
    zone_id: Optional[int] = Field(default=None, foreign_key="zone.id")
    kind: str                                # low_delivery | over_delivery | leak | moisture_response | sensor | ...
    severity: str = "warning"                # warning | critical
    message: str
    blocks_irrigation: bool = True
    acknowledged: bool = False
    acknowledged_at: Optional[NaiveDatetime] = None


class Identification(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ts: NaiveDatetime = Field(default_factory=utcnow)
    zone_id: int = Field(foreign_key="zone.id", index=True)
    species_key: Optional[str] = None
    species_label: Optional[str] = None
    growth_stage: Optional[str] = None
    confidence: Optional[float] = None
    source: str = "ai"                       # ai | farmer
    model_name: Optional[str] = None
    image_ref: Optional[str] = None
    status: str = "needs_confirmation"       # needs_confirmation | confirmed | rejected
    confirmed_at: Optional[NaiveDatetime] = None


class SystemState(SQLModel, table=True):
    id: Optional[int] = Field(default=1, primary_key=True)
    mode: str = "auto"                       # auto | manual (manual = no automatic irrigation)
    emergency_stop: bool = False
    emergency_stop_at: Optional[NaiveDatetime] = None
    controller_last_seen: Optional[NaiveDatetime] = None


class VisionObservation(SQLModel, table=True):
    """One analysed image from the vision service (mast, carriage, phone or sticky trap)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    ts: NaiveDatetime = Field(default_factory=utcnow, index=True)
    zone_id: Optional[int] = Field(default=None, foreign_key="zone.id", index=True)
    plant_ref: Optional[str] = None          # e.g. "Z1-P04" when the image shows one plant
    source: str = "mast"                     # mast | carriage | phone | trap
    image_ref: Optional[str] = None
    canopy_cover_pct: Optional[float] = None
    crop_key: Optional[str] = None
    crop_confidence: Optional[float] = None
    growth_stage: Optional[str] = None
    stage_confidence: Optional[float] = None
    raining_now: Optional[bool] = None
    rain_confidence: Optional[float] = None
    trap_insect_count: Optional[int] = None
    engines: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class Plant(SQLModel, table=True):
    """An individual plant, followed with its own history (needed for AGRIWISE PRECISION).

    In AGRIWISE SMART plants still belong to a zone and are watered by the zone's valve:
    `has_own_valve` stays False and no individual command is ever sent for them.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    zone_id: int = Field(foreign_key="zone.id", index=True)
    label: str = Field(index=True)                 # e.g. "Z1-P04" (as written on the stake / sent by the camera)
    crop_profile_id: Optional[int] = Field(default=None, foreign_key="cropprofile.id")  # None = zone's crop
    planting_date: Optional[date] = None           # None = zone's planting date
    has_own_valve: bool = False                    # True only if the hydraulics really allow it
    valve_channel: Optional[int] = None
    active: bool = True
    created_at: NaiveDatetime = Field(default_factory=utcnow)


class StageObservation(SQLModel, table=True):
    """One piece of evidence about a growth stage: AI photo, farmer confirmation or a recorded transition."""
    id: Optional[int] = Field(default=None, primary_key=True)
    ts: NaiveDatetime = Field(default_factory=utcnow, index=True)
    observed_on: date = Field(default_factory=lambda: utcnow().date())
    zone_id: int = Field(foreign_key="zone.id", index=True)
    plant_id: Optional[int] = Field(default=None, foreign_key="plant.id", index=True)
    crop_key: str
    source: str                                    # ai | farmer | system
    visual_class: Optional[str] = None             # what the camera recognised (ai)
    candidate_stages: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    stage_key: Optional[str] = None                # resolved agronomic stage (None while ambiguous)
    confidence: Optional[float] = None
    visual_cues: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    status: str = "estimated"                      # estimated | needs_confirmation | confirmed | rejected | superseded
    image_ref: Optional[str] = None
    vision_observation_id: Optional[int] = None
    note: Optional[str] = None
