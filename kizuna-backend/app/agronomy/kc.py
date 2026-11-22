"""Single crop coefficient curve (FAO-56 Chapter 6)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KcCurve:
    kc_ini: float
    kc_mid: float
    kc_end: float
    l_ini: int
    l_dev: int
    l_mid: int
    l_late: int

    @property
    def season_length(self) -> int:
        return self.l_ini + self.l_dev + self.l_mid + self.l_late


STAGES = ("initial", "development", "mid_season", "late_season", "after_season")


@dataclass(frozen=True)
class StageInfo:
    stage: str            # one of STAGES
    day: int              # days after planting (0 = planting day)
    days_in_stage: int
    days_to_next_stage: int | None


def stage_for_day(curve: KcCurve, day: int) -> StageInfo:
    bounds = [
        ("initial", 0, curve.l_ini),
        ("development", curve.l_ini, curve.l_ini + curve.l_dev),
        ("mid_season", curve.l_ini + curve.l_dev, curve.l_ini + curve.l_dev + curve.l_mid),
        ("late_season", curve.l_ini + curve.l_dev + curve.l_mid, curve.season_length),
    ]
    if day < 0:
        return StageInfo("initial", day, 0, -day)
    for name, start, end in bounds:
        if start <= day < end:
            return StageInfo(name, day, day - start, end - day)
    return StageInfo("after_season", day, day - curve.season_length, None)


def kc_for_day(curve: KcCurve, day: int, kc_mid: float | None = None, kc_end: float | None = None) -> float:
    """Kc on a given day after planting (Eq. 66: linear interpolation in dev and late stages)."""
    mid = curve.kc_mid if kc_mid is None else kc_mid
    end = curve.kc_end if kc_end is None else kc_end
    d = max(day, 0)
    if d < curve.l_ini:
        return curve.kc_ini
    if d < curve.l_ini + curve.l_dev:
        frac = (d - curve.l_ini) / curve.l_dev
        return curve.kc_ini + frac * (mid - curve.kc_ini)
    if d < curve.l_ini + curve.l_dev + curve.l_mid:
        return mid
    if d < curve.season_length:
        frac = (d - curve.l_ini - curve.l_dev - curve.l_mid) / curve.l_late
        return mid + frac * (end - mid)
    return end


def _climate_term(u2: float, rh_min: float, height_m: float) -> float:
    u2c = min(max(u2, 1.0), 6.0)          # Eq. 62 validity: 1 <= u2 <= 6 m/s
    rhc = min(max(rh_min, 20.0), 80.0)    # 20 <= RHmin <= 80 %
    hc = min(max(height_m, 0.1), 10.0)    # 0.1 < h < 10 m
    return (0.04 * (u2c - 2.0) - 0.004 * (rhc - 45.0)) * (hc / 3.0) ** 0.3


def adjust_kc_mid(kc_mid_table: float, u2: float, rh_min: float, height_m: float) -> float:
    """Eq. 62: climatic adjustment of Kc mid (table values assume RHmin = 45 %, u2 = 2 m/s)."""
    return kc_mid_table + _climate_term(u2, rh_min, height_m)


def adjust_kc_end(kc_end_table: float, u2: float, rh_min: float, height_m: float) -> float:
    """Eq. 65: only applied when Kc end (Tab) exceeds 0.45."""
    if kc_end_table < 0.45:
        return kc_end_table
    return kc_end_table + _climate_term(u2, rh_min, height_m)


def root_depth_for_day(zr_ini: float, zr_max: float, curve: KcCurve, day: int) -> float:
    """Root depth grows linearly from planting to the start of mid-season, then stays at max.

    Simplification consistent with FAO-56 Chapter 8 (variable root depth); edit
    zr_ini/zr_max per field if measured.
    """
    full_at = curve.l_ini + curve.l_dev
    d = max(day, 0)
    if d >= full_at:
        return zr_max
    return zr_ini + (zr_max - zr_ini) * d / full_at
