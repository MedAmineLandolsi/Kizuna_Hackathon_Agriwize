"""Root-zone water balance and irrigation requirement (FAO-56 Chapter 8).

1 mm of water over 1 m2 = 1 litre.
"""
from __future__ import annotations

from dataclasses import dataclass


def total_available_water(theta_fc: float, theta_wp: float, zr_m: float) -> float:
    """Eq. 82: TAW (mm)."""
    return 1000.0 * (theta_fc - theta_wp) * zr_m


def adjusted_depletion_fraction(p_table: float, etc_mm: float) -> float:
    """Table 22 footnote: p = p_table + 0.04 (5 - ETc), limited to 0.1 <= p <= 0.8."""
    return min(max(p_table + 0.04 * (5.0 - etc_mm), 0.1), 0.8)


def readily_available_water(p: float, taw: float) -> float:
    """Eq. 83: RAW (mm)."""
    return p * taw


def depletion_from_soil_moisture(theta: float, theta_fc: float, zr_m: float, taw: float) -> float:
    """Root-zone depletion (mm) from a measured volumetric water content, limited to 0..TAW (Eq. 86)."""
    dr = 1000.0 * (theta_fc - theta) * zr_m
    return min(max(dr, 0.0), taw)


def water_stress_coefficient(dr: float, taw: float, raw: float, p: float) -> float:
    """Eq. 84: Ks."""
    if dr <= raw:
        return 1.0
    if taw <= 0:
        return 0.0
    return max((taw - dr) / ((1.0 - p) * taw), 0.0)


def effective_rainfall(precip_mm: float, et0_mm: float) -> float:
    """FAO-56 Ch. 8: daily rain below ~0.2 ET0 is normally entirely evaporated and ignored.

    Surface runoff is not modelled (drip-irrigated, low-slope plots); any rain
    above the root-zone deficit is counted as deep percolation in daily_step().
    """
    if precip_mm <= 0 or precip_mm < 0.2 * et0_mm:
        return 0.0
    return precip_mm


@dataclass(frozen=True)
class BalanceStep:
    dr_end: float
    deep_percolation: float


def daily_step(dr_start: float, *, etc_mm: float, rain_eff_mm: float, irrigation_net_mm: float, taw: float) -> BalanceStep:
    """Eq. 85 with RO = 0 and CR = 0; DP is the water pushed above field capacity."""
    dr = dr_start - rain_eff_mm - irrigation_net_mm + etc_mm
    dp = 0.0
    if dr < 0:
        dp = -dr
        dr = 0.0
    return BalanceStep(dr_end=min(dr, taw), deep_percolation=dp)


def net_irrigation_mm(dr: float, refill_fraction: float = 1.0) -> float:
    """Net depth needed to bring the root zone back towards field capacity."""
    return max(dr * refill_fraction, 0.0)


def gross_irrigation_mm(net_mm: float, efficiency: float) -> float:
    if efficiency <= 0:
        raise ValueError("efficiency must be > 0")
    return net_mm / efficiency


def mm_to_litres(depth_mm: float, area_m2: float) -> float:
    return depth_mm * area_m2


def litres_to_mm(volume_l: float, area_m2: float) -> float:
    return volume_l / area_m2 if area_m2 > 0 else 0.0
