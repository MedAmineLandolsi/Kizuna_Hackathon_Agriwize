"""Thermal time (growing degree-days, GDD).

Method: single sine with horizontal cutoff (Baskerville & Emin 1969), the method used by the
Oregon State University Croptime vegetable models [OSU_CROPTIME]. The daily temperature is
assumed to follow a sine curve between Tmin and Tmax; only the part between the lower and the
upper threshold counts.

How AGRIWISE uses degree-days (see growth.py):
No stage requirement in degree-days is invented. Published thermal requirements are specific to
a variety and a site (Croptime models are calibrated per variety in Oregon), so they cannot be
copied for a Tunisian farm. Instead, the FAO-56 stage lengths are taken as the development of the
crop in a NORMAL local season, and the degree-days of the actual season are compared with the
degree-days of the local climate normals over the same dates:
    warmer than normal -> the crop is ahead of the calendar; colder -> behind.
The thresholds therefore only shape the temperature response; the stage lengths stay the FAO ones.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class ThermalParams:
    t_base_c: float     # lower development threshold
    t_upper_c: float    # upper threshold (horizontal cutoff)
    source: str         # reference key(s), see docs/REFERENCES.md
    note: str = ""


def gdd_single_sine(tmin: float, tmax: float, t_base: float, t_upper: float) -> float:
    """Degree-days for one day, single sine method with horizontal cutoff (°C·day)."""
    if tmax < tmin:
        tmin, tmax = tmax, tmin
    if t_upper <= t_base:
        raise ValueError("t_upper must be above t_base")
    if tmax <= t_base:                       # whole day below the lower threshold
        return 0.0
    if tmin >= t_upper:                      # whole day above the upper threshold
        return t_upper - t_base
    mean = (tmax + tmin) / 2.0
    if tmax == tmin:
        return min(max(mean, t_base), t_upper) - t_base
    amp = (tmax - tmin) / 2.0
    half_pi = math.pi / 2.0
    if tmin >= t_base and tmax <= t_upper:   # entirely between the thresholds
        return mean - t_base
    if tmin < t_base and tmax <= t_upper:    # crosses the lower threshold only
        th1 = math.asin((t_base - mean) / amp)
        return ((mean - t_base) * (half_pi - th1) + amp * math.cos(th1)) / math.pi
    if tmin >= t_base and tmax > t_upper:    # crosses the upper threshold only
        th2 = math.asin((t_upper - mean) / amp)
        return ((mean - t_base) * (th2 + half_pi) + (t_upper - t_base) * (half_pi - th2)
                - amp * math.cos(th2)) / math.pi
    # crosses both thresholds
    th1 = math.asin((t_base - mean) / amp)
    th2 = math.asin((t_upper - mean) / amp)
    return ((mean - t_base) * (th2 - th1) + amp * (math.cos(th1) - math.cos(th2))
            + (t_upper - t_base) * (half_pi - th2)) / math.pi


def gdd(tmin: float, tmax: float, params: ThermalParams) -> float:
    return gdd_single_sine(tmin, tmax, params.t_base_c, params.t_upper_c)


def equivalent_normal_days(target_gdd: float, normal_gdd_series: list[float]) -> float | None:
    """Number of NORMAL days (fractional) needed to accumulate `target_gdd`.

    `normal_gdd_series` = normal degree-days of consecutive dates from the reference start.
    Returns None if the series is too short.
    """
    if target_gdd <= 0:
        return 0.0
    acc = 0.0
    for i, g in enumerate(normal_gdd_series):
        if acc + g >= target_gdd:
            return i + ((target_gdd - acc) / g if g > 0 else 0.0)
        acc += g
    return None
