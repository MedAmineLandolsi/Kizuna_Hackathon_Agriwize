"""Reference evapotranspiration (ET0) following FAO Irrigation and Drainage Paper 56.

All equation numbers refer to FAO-56 (Allen et al., 1998).
Units: temperature degC, radiation MJ m-2 day-1, pressure kPa, wind m/s,
ET0 mm/day.
"""
from __future__ import annotations

import math

SIGMA = 4.903e-9  # Stefan-Boltzmann constant, MJ K-4 m-2 day-1
GSC = 0.0820      # solar constant, MJ m-2 min-1


def atmospheric_pressure(altitude_m: float) -> float:
    """Eq. 7."""
    return 101.3 * ((293.0 - 0.0065 * altitude_m) / 293.0) ** 5.26


def psychrometric_constant(pressure_kpa: float) -> float:
    """Eq. 8."""
    return 0.665e-3 * pressure_kpa


def saturation_vapour_pressure(t: float) -> float:
    """Eq. 11, e°(T) in kPa."""
    return 0.6108 * math.exp(17.27 * t / (t + 237.3))


def slope_vapour_pressure_curve(t: float) -> float:
    """Eq. 13, delta in kPa/degC."""
    return 4098.0 * saturation_vapour_pressure(t) / (t + 237.3) ** 2


def mean_saturation_vapour_pressure(tmax: float, tmin: float) -> float:
    """Eq. 12."""
    return (saturation_vapour_pressure(tmax) + saturation_vapour_pressure(tmin)) / 2.0


def actual_vapour_pressure_from_rh(tmax: float, tmin: float, rh_max: float, rh_min: float) -> float:
    """Eq. 17."""
    return (saturation_vapour_pressure(tmin) * rh_max / 100.0
            + saturation_vapour_pressure(tmax) * rh_min / 100.0) / 2.0


def actual_vapour_pressure_from_rh_mean(tmax: float, tmin: float, rh_mean: float) -> float:
    """Eq. 19 (used when only mean RH is available)."""
    return rh_mean / 100.0 * mean_saturation_vapour_pressure(tmax, tmin)


def wind_speed_2m(uz: float, z: float) -> float:
    """Eq. 47: convert wind speed measured at height z (m) to 2 m."""
    return uz * 4.87 / math.log(67.8 * z - 5.42)


def _solar_geometry(latitude_deg: float, day_of_year: int) -> tuple[float, float, float, float]:
    phi = math.radians(latitude_deg)
    dr = 1.0 + 0.033 * math.cos(2.0 * math.pi * day_of_year / 365.0)          # Eq. 23
    delta = 0.409 * math.sin(2.0 * math.pi * day_of_year / 365.0 - 1.39)      # Eq. 24
    x = -math.tan(phi) * math.tan(delta)
    ws = math.acos(max(-1.0, min(1.0, x)))                                     # Eq. 25
    return phi, dr, delta, ws


def extraterrestrial_radiation(latitude_deg: float, day_of_year: int) -> float:
    """Eq. 21, Ra in MJ m-2 day-1."""
    phi, dr, delta, ws = _solar_geometry(latitude_deg, day_of_year)
    return (24.0 * 60.0 / math.pi) * GSC * dr * (
        ws * math.sin(phi) * math.sin(delta) + math.cos(phi) * math.cos(delta) * math.sin(ws)
    )


def daylight_hours(latitude_deg: float, day_of_year: int) -> float:
    """Eq. 34."""
    _, _, _, ws = _solar_geometry(latitude_deg, day_of_year)
    return 24.0 / math.pi * ws


def solar_radiation_from_sunshine(ra: float, n_hours: float, n_max_hours: float,
                                  a_s: float = 0.25, b_s: float = 0.50) -> float:
    """Eq. 35 (Angstrom)."""
    return (a_s + b_s * n_hours / n_max_hours) * ra


def solar_radiation_from_temperature(ra: float, tmax: float, tmin: float, coastal: bool = False) -> float:
    """Eq. 50 (Hargreaves radiation formula). kRs = 0.16 interior, 0.19 coastal."""
    k_rs = 0.19 if coastal else 0.16
    return k_rs * math.sqrt(max(tmax - tmin, 0.0)) * ra


def clear_sky_radiation(ra: float, altitude_m: float) -> float:
    """Eq. 37."""
    return (0.75 + 2e-5 * altitude_m) * ra


def net_shortwave_radiation(rs: float, albedo: float = 0.23) -> float:
    """Eq. 38."""
    return (1.0 - albedo) * rs


def net_longwave_radiation(tmax: float, tmin: float, ea: float, rs: float, rso: float) -> float:
    """Eq. 39."""
    tmax_k4 = (tmax + 273.16) ** 4
    tmin_k4 = (tmin + 273.16) ** 4
    ratio = min(rs / rso, 1.0) if rso > 0 else 1.0
    return SIGMA * (tmax_k4 + tmin_k4) / 2.0 * (0.34 - 0.14 * math.sqrt(ea)) * (1.35 * ratio - 0.35)


def et0_penman_monteith(
    *,
    tmax: float,
    tmin: float,
    u2: float,
    latitude_deg: float,
    altitude_m: float,
    day_of_year: int,
    rh_max: float | None = None,
    rh_min: float | None = None,
    rh_mean: float | None = None,
    rs: float | None = None,
    sunshine_hours: float | None = None,
    coastal: bool = False,
    g: float = 0.0,
) -> float:
    """FAO Penman-Monteith daily ET0 (Eq. 6), mm/day.

    Solar radiation priority: measured rs > sunshine hours > Hargreaves estimate (Eq. 50).
    Vapour pressure priority: RHmax+RHmin (Eq. 17) > RHmean (Eq. 19) > Tmin as dew point (Eq. 48).
    """
    tmean = (tmax + tmin) / 2.0
    pressure = atmospheric_pressure(altitude_m)
    gamma = psychrometric_constant(pressure)
    delta = slope_vapour_pressure_curve(tmean)
    es = mean_saturation_vapour_pressure(tmax, tmin)
    if rh_max is not None and rh_min is not None:
        ea = actual_vapour_pressure_from_rh(tmax, tmin, rh_max, rh_min)
    elif rh_mean is not None:
        ea = actual_vapour_pressure_from_rh_mean(tmax, tmin, rh_mean)
    else:
        ea = saturation_vapour_pressure(tmin)  # Eq. 48, Tdew ~ Tmin

    ra = extraterrestrial_radiation(latitude_deg, day_of_year)
    if rs is None:
        if sunshine_hours is not None:
            rs = solar_radiation_from_sunshine(ra, sunshine_hours, daylight_hours(latitude_deg, day_of_year))
        else:
            rs = solar_radiation_from_temperature(ra, tmax, tmin, coastal)
    rso = clear_sky_radiation(ra, altitude_m)
    rn = net_shortwave_radiation(rs) - net_longwave_radiation(tmax, tmin, ea, rs, rso)

    numerator = 0.408 * delta * (rn - g) + gamma * (900.0 / (tmean + 273.0)) * u2 * (es - ea)
    denominator = delta + gamma * (1.0 + 0.34 * u2)
    return max(numerator / denominator, 0.0)


def et0_hargreaves(*, tmax: float, tmin: float, latitude_deg: float, day_of_year: int) -> float:
    """Eq. 52 (Hargreaves). FAO-56 fallback when only air temperature is available."""
    ra = extraterrestrial_radiation(latitude_deg, day_of_year)
    tmean = (tmax + tmin) / 2.0
    return max(0.0023 * (tmean + 17.8) * math.sqrt(max(tmax - tmin, 0.0)) * 0.408 * ra, 0.0)
