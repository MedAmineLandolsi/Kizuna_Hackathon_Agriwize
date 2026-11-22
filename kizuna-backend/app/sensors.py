"""Sensor calibration and validation.

Rule from the specification: never irrigate automatically on invalid sensor data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import SensorReading, Zone

ADC_MIN, ADC_MAX = 0, 4095          # ESP32 12-bit ADC
THETA_MIN, THETA_MAX = 0.0, 0.60    # physically plausible volumetric water content


@dataclass
class ValidationResult:
    valid: bool
    issues: list[str] = field(default_factory=list)


def raw_to_theta(zone: Zone, raw: float) -> float | None:
    """Two-point linear calibration (capacitive sensors read lower raw values when wetter)."""
    if None in (zone.sensor_raw_dry, zone.sensor_theta_dry, zone.sensor_raw_wet, zone.sensor_theta_wet):
        return None
    if zone.sensor_raw_dry == zone.sensor_raw_wet:
        return None
    slope = (zone.sensor_theta_wet - zone.sensor_theta_dry) / (zone.sensor_raw_wet - zone.sensor_raw_dry)
    return zone.sensor_theta_dry + slope * (raw - zone.sensor_raw_dry)


def validate_reading(r: SensorReading) -> ValidationResult:
    issues: list[str] = []
    if r.soil_raw is not None and (r.soil_raw <= ADC_MIN or r.soil_raw >= ADC_MAX):
        issues.append("soil sensor raw value at ADC limit (disconnected or short circuit)")
    if r.soil_theta is not None and not (THETA_MIN <= r.soil_theta <= THETA_MAX):
        issues.append(f"soil moisture {r.soil_theta:.3f} m3/m3 outside plausible range")
    if r.air_temp_c is not None and not (-10 <= r.air_temp_c <= 60):
        issues.append(f"air temperature {r.air_temp_c} C implausible")
    if r.soil_temp_c is not None and not (-10 <= r.soil_temp_c <= 60):
        issues.append(f"soil temperature {r.soil_temp_c} C implausible")
    if r.air_rh_pct is not None and not (0 <= r.air_rh_pct <= 100):
        issues.append(f"air humidity {r.air_rh_pct} % implausible")
    if r.tank_level_pct is not None and not (0 <= r.tank_level_pct <= 100):
        issues.append(f"tank level {r.tank_level_pct} % implausible")
    if r.flow_lpm is not None and r.flow_lpm < 0:
        issues.append("negative flow")
    if r.soil_ec_ds_m is not None and not (0 <= r.soil_ec_ds_m <= 30):
        issues.append(f"soil EC {r.soil_ec_ds_m} dS/m implausible")
    return ValidationResult(valid=not issues, issues=issues)


def is_fresh(r: SensorReading | None, now: datetime, max_age_min: float) -> bool:
    return r is not None and (now - r.ts) <= timedelta(minutes=max_age_min)
