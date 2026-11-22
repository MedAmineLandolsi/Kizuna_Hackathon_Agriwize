"""Crop and soil reference data from FAO-56 (Allen et al., 1998).

These are the *defaults* loaded into the database. Farmers/agronomists can
edit crop profiles through the API (e.g. to use local Tunisian data from
INRAT / CRDA). Every value carries its source so the dashboard can show it.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CropReference:
    key: str
    name: str
    kc_ini: float
    kc_mid: float
    kc_end: float
    max_height_m: float
    # Stage lengths in days: initial, development, mid-season, late season
    l_ini: int
    l_dev: int
    l_mid: int
    l_late: int
    zr_max_m: float          # maximum effective root depth used for scheduling
    zr_ini_m: float          # root depth at planting/transplanting
    p: float                 # depletion fraction for no stress (Table 22)
    season_label: str
    source: str
    notes: list[str] = field(default_factory=list)


CROPS: dict[str, CropReference] = {
    "tomato": CropReference(
        key="tomato",
        name="Tomato",
        kc_ini=0.60, kc_mid=1.15, kc_end=0.80,
        max_height_m=0.6,
        l_ini=30, l_dev=40, l_mid=45, l_late=30,
        zr_max_m=0.7, zr_ini_m=0.2,
        p=0.40,
        season_label="April/May planting, Mediterranean",
        source="FAO-56 Table 11 (stages), Table 12 (Kc, height), Table 22 (Zr, p)",
        notes=[
            "Kc end is 0.70-0.90 in Table 12; 0.80 (mid-range) is used by default.",
            "Zr uses the lower end of the 0.7-1.5 m range, as is common for irrigation scheduling.",
            "Staked tomatoes (1.5-2 m): Table 12 footnote suggests Kc mid = 1.20.",
            "Initial root depth 0.2 m is an assumption for transplanted seedlings.",
        ],
    ),
    "pepper": CropReference(
        key="pepper",
        name="Sweet pepper (bell)",
        kc_ini=0.60, kc_mid=1.05, kc_end=0.90,
        max_height_m=0.7,
        l_ini=25, l_dev=35, l_mid=40, l_late=20,
        zr_max_m=0.5, zr_ini_m=0.2,
        p=0.30,
        season_label="April/June planting, Europe and Mediterranean",
        source="FAO-56 Table 11 (stages), Table 12 (Kc, height), Table 22 (Zr, p)",
        notes=[
            "Zr uses the lower end of the 0.5-1.0 m range.",
            "Staked peppers: Table 12 footnote suggests Kc mid = 1.15.",
            "Initial root depth 0.2 m is an assumption for transplanted seedlings.",
        ],
    ),
    "potato": CropReference(
        key="potato",
        name="Potato",
        kc_ini=0.50, kc_mid=1.15, kc_end=0.75,
        max_height_m=0.6,
        l_ini=25, l_dev=30, l_mid=45, l_late=30,
        zr_max_m=0.4, zr_ini_m=0.15,
        p=0.35,
        season_label="Jan/Nov planting, (semi) arid climate",
        source="FAO-56 Table 11 (stages), Table 12 (Kc, height), Table 22 (Zr, p)",
        notes=["Table 11 gives 30 or 45 days of mid-season for this region; 45 is used.",
               "Kc end is about 0.40 for long-season potatoes with vine kill (Table 12 footnote).",
               "Zr uses the lower end of the 0.4-0.6 m range; initial depth 0.15 m is an assumption."],
    ),
    "eggplant": CropReference(
        key="eggplant",
        name="Eggplant",
        kc_ini=0.60, kc_mid=1.05, kc_end=0.90,
        max_height_m=0.8,
        l_ini=30, l_dev=45, l_mid=40, l_late=25,
        zr_max_m=0.7, zr_ini_m=0.2,
        p=0.45,
        season_label="May/June planting, Mediterranean",
        source="FAO-56 Table 11 (stages), Table 12 (Kc, height), Table 22 (Zr, p)",
        notes=["Zr uses the lower end of the 0.7-1.2 m range; initial depth 0.2 m is an assumption."],
    ),
}

# Alternative stage lengths from FAO-56 Table 11 for other planting seasons.
STAGE_ALTERNATIVES: dict[str, list[dict]] = {
    "tomato": [
        {"label": "January planting, arid region", "l_ini": 30, "l_dev": 40, "l_mid": 40, "l_late": 25},
        {"label": "April/May planting, Mediterranean", "l_ini": 30, "l_dev": 40, "l_mid": 45, "l_late": 30},
        {"label": "Oct/Nov planting, arid region", "l_ini": 35, "l_dev": 45, "l_mid": 70, "l_late": 30},
    ],
    "pepper": [
        {"label": "April/June planting, Europe and Mediterranean", "l_ini": 25, "l_dev": 35, "l_mid": 40, "l_late": 20},
        {"label": "October planting, arid region", "l_ini": 30, "l_dev": 40, "l_mid": 110, "l_late": 30},
    ],
}


@dataclass(frozen=True)
class SoilReference:
    key: str
    name: str
    theta_fc: float  # field capacity, m3/m3 (mid-range of Table 19)
    theta_wp: float  # wilting point, m3/m3 (mid-range of Table 19)
    fc_range: tuple[float, float]
    wp_range: tuple[float, float]
    source: str = "FAO-56 Table 19 (mid-range values)"


def _soil(key: str, name: str, fc: tuple[float, float], wp: tuple[float, float]) -> SoilReference:
    return SoilReference(
        key=key, name=name,
        theta_fc=round((fc[0] + fc[1]) / 2, 3),
        theta_wp=round((wp[0] + wp[1]) / 2, 3),
        fc_range=fc, wp_range=wp,
    )


SOILS: dict[str, SoilReference] = {s.key: s for s in [
    _soil("sand", "Sand", (0.07, 0.17), (0.02, 0.07)),
    _soil("loamy_sand", "Loamy sand", (0.11, 0.19), (0.03, 0.10)),
    _soil("sandy_loam", "Sandy loam", (0.18, 0.28), (0.06, 0.16)),
    _soil("loam", "Loam", (0.20, 0.30), (0.07, 0.17)),
    _soil("silt_loam", "Silt loam", (0.22, 0.36), (0.09, 0.21)),
    _soil("silt", "Silt", (0.28, 0.36), (0.12, 0.22)),
    _soil("silt_clay_loam", "Silt clay loam", (0.30, 0.37), (0.17, 0.24)),
    _soil("silty_clay", "Silty clay", (0.30, 0.42), (0.17, 0.29)),
    _soil("clay", "Clay", (0.32, 0.40), (0.20, 0.24)),
]}

# Typical application efficiencies. Replace with a field measurement when possible.
IRRIGATION_EFFICIENCY: dict[str, float] = {
    "drip": 0.90,
    "sprinkler": 0.75,
    "surface": 0.60,
}
EFFICIENCY_SOURCE = "Typical values (FAO Irrigation Water Management training manuals); measure on site when possible"
