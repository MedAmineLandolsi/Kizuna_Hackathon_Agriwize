"""Daily temperatures for thermal time: actual season and local climate normals.

Actual temperature of a past day, best source first:
  1. measured  - on-farm air temperature sensor (daily min/max, enough readings over the day)
  2. forecast  - Open-Meteo forecast model value stored in WeatherDay (past and current days)
  3. reanalysis - Open-Meteo historical archive (ERA5) stored in ClimateDay
A day with none of these is MISSING: it is counted as a normal day and reduces the coverage.

Climate normals: mean degree-days of the same calendar date over the archived years (at least
MIN_NORMAL_YEARS). No normal -> no thermal adjustment (the calendar is used and the reason shown).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlmodel import Session, select

from .agronomy.thermal import ThermalParams, equivalent_normal_days, gdd
from .config import settings
from .models import ClimateDay, SensorReading, WeatherDay, utcnow

log = logging.getLogger(__name__)

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
MIN_NORMAL_YEARS = 3
NORMAL_YEARS = 5
MIN_COVERAGE = 0.8            # share of elapsed days that must have a real temperature
SENSOR_MIN_READINGS = 4
SENSOR_MIN_SPAN_H = 12.0
ARCHIVE_DELAY_DAYS = 7        # the reanalysis archive runs several days behind real time
LAST_ERROR: str | None = None  # reason of the last failed download, shown by the API


# ------------------------------------------------------------------ archive download
def parse_archive(payload: dict) -> list[ClimateDay]:
    daily = payload.get("daily", {})
    out = []
    for i, d in enumerate(daily.get("time", [])):
        tmax = (daily.get("temperature_2m_max") or [None] * (i + 1))[i]
        tmin = (daily.get("temperature_2m_min") or [None] * (i + 1))[i]
        if tmax is None or tmin is None:
            continue                                  # recent days not yet in the reanalysis
        out.append(ClimateDay(day=date.fromisoformat(d), tmax_c=tmax, tmin_c=tmin))
    return out


def store_climate_days(session: Session, days: list[ClimateDay]) -> int:
    existing = {c.day: c for c in session.exec(select(ClimateDay)).all()}
    for cd in days:
        row = existing.get(cd.day)
        if row:
            row.tmax_c, row.tmin_c, row.source, row.fetched_at = cd.tmax_c, cd.tmin_c, cd.source, utcnow()
            session.add(row)
        else:
            session.add(cd)
    session.commit()
    return len(days)


def refresh_climate(session: Session, years: int = NORMAL_YEARS, today: date | None = None,
                    client: httpx.Client | None = None) -> int:
    """Download daily Tmax/Tmin from 1 January `years` years ago up to a week ago. 0 if it fails
    (the reason is kept in LAST_ERROR)."""
    global LAST_ERROR
    today = today or datetime.now(ZoneInfo(settings.timezone)).date()
    params = {
        "latitude": settings.farm_latitude, "longitude": settings.farm_longitude,
        "start_date": date(today.year - years, 1, 1).isoformat(),
        "end_date": (today - timedelta(days=ARCHIVE_DELAY_DAYS)).isoformat(),
        "daily": "temperature_2m_max,temperature_2m_min", "timezone": settings.timezone,
    }
    own = client is None
    client = client or httpx.Client(timeout=30)
    try:
        resp = client.get(ARCHIVE_URL, params=params)
        if resp.status_code >= 400:
            LAST_ERROR = f"Open-Meteo answered {resp.status_code}: {resp.text[:300]}"
            log.warning("Climate archive download failed: %s", LAST_ERROR)
            return 0
        days = parse_archive(resp.json())
    except (httpx.HTTPError, ValueError) as exc:
        LAST_ERROR = f"{type(exc).__name__}: {exc}"
        log.warning("Climate archive download failed (offline?): %s", LAST_ERROR)
        return 0
    finally:
        if own:
            client.close()
    LAST_ERROR = None
    return store_climate_days(session, days)


def climate_needs_refresh(session: Session, today: date) -> bool:
    last = session.exec(select(ClimateDay).order_by(ClimateDay.fetched_at.desc())).first()
    return last is None or (utcnow() - last.fetched_at) > timedelta(hours=24)


# ------------------------------------------------------------------ temperature sources
@dataclass
class DayTemp:
    tmin: float
    tmax: float
    kind: str          # measured | forecast | reanalysis
    source: str


@dataclass
class TemperatureData:
    """All temperatures needed for one assessment, loaded once."""
    sensor: dict[date, DayTemp] = field(default_factory=dict)
    forecast: dict[date, DayTemp] = field(default_factory=dict)
    archive: dict[date, DayTemp] = field(default_factory=dict)
    # simulated temperatures (step 6 – time simulation); never mixed with real data
    simulated: dict[date, DayTemp] | None = None

    def actual(self, d: date) -> DayTemp | None:
        if self.simulated is not None:
            return self.simulated.get(d)
        return self.sensor.get(d) or self.forecast.get(d) or self.archive.get(d)

    def normal_years(self) -> int:
        return len({d.year for d in self.archive})

    def normal_gdd(self, d: date, params: ThermalParams) -> float | None:
        """Mean degree-days of this calendar date over the archived years (excluding d's own year
        when it is the current season, so the normal is not the season itself)."""
        md = (d.month, 28 if (d.month, d.day) == (2, 29) else d.day)
        vals = []
        for y in self._years():
            if y == d.year:
                continue
            try:
                t = self.archive.get(date(y, *md))
            except ValueError:
                t = None
            if t:
                vals.append(gdd(t.tmin, t.tmax, params))
        if len(vals) < MIN_NORMAL_YEARS:
            return None
        return sum(vals) / len(vals)

    def _years(self) -> list[int]:
        if not hasattr(self, "_year_cache"):
            self._year_cache = sorted({d.year for d in self.archive})
        return self._year_cache


def _local_day_bounds_utc(d: date, tz: str) -> tuple[datetime, datetime]:
    z = ZoneInfo(tz)
    start = datetime.combine(d, time(0), tzinfo=z).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return start, start + timedelta(days=1)


def load_temperatures(session: Session, start: date, end: date, tz: str | None = None) -> TemperatureData:
    """Temperatures for [start, end] (actual sources) + the whole archive (normals)."""
    tz = tz or settings.timezone
    data = TemperatureData()
    for c in session.exec(select(ClimateDay)).all():
        if c.tmin_c is not None and c.tmax_c is not None:
            data.archive[c.day] = DayTemp(c.tmin_c, c.tmax_c, "reanalysis", "Open-Meteo historical archive (ERA5)")
    for w in session.exec(select(WeatherDay).where(WeatherDay.day >= start, WeatherDay.day <= end)).all():
        if w.tmin_c is not None and w.tmax_c is not None:
            data.forecast[w.day] = DayTemp(w.tmin_c, w.tmax_c, "forecast", "Open-Meteo forecast model")
    s_utc, _ = _local_day_bounds_utc(start, tz)
    _, e_utc = _local_day_bounds_utc(end, tz)
    rows = session.exec(select(SensorReading.ts, SensorReading.air_temp_c)
                        .where(SensorReading.ts >= s_utc, SensorReading.ts < e_utc,
                               SensorReading.air_temp_c.is_not(None), SensorReading.valid == True)).all()  # noqa: E712
    by_day: dict[date, list[tuple[datetime, float]]] = defaultdict(list)
    z = ZoneInfo(tz)
    for ts, t in rows:
        local = ts.replace(tzinfo=ZoneInfo("UTC")).astimezone(z)
        by_day[local.date()].append((ts, t))
    for d, vals in by_day.items():
        span_h = (max(v[0] for v in vals) - min(v[0] for v in vals)).total_seconds() / 3600
        if len(vals) >= SENSOR_MIN_READINGS and span_h >= SENSOR_MIN_SPAN_H:
            temps = [v[1] for v in vals]
            data.sensor[d] = DayTemp(min(temps), max(temps), "measured", "on-farm air temperature sensor")
    return data


# ------------------------------------------------------------------ thermal progress
def thermal_progress(data: TemperatureData, params: ThermalParams, start: date, today: date,
                     norm_start: date | None = None) -> dict[str, Any]:
    """Compare the degree-days actually accumulated since `start` with the reference ones.

    `norm_start`: date, in the crop's REFERENCE season, matching the crop's position at `start`
    (e.g. planting in mid-May for pepper). The reference degree-days are the local climate normals
    from that date. Default: the same dates as the season (no reference season).

    Returns `equivalent_days`: how many REFERENCE days of development the crop has accumulated since
    `start` (the calendar would say `days_elapsed`).
    """
    norm_start = norm_start or start
    elapsed = (today - start).days                    # complete days, today excluded
    out: dict[str, Any] = {"available": False, "days_elapsed": elapsed, "t_base_c": params.t_base_c,
                           "t_upper_c": params.t_upper_c, "method": "single sine, horizontal cutoff",
                           "params_source": params.source}
    if data.simulated is None and data.normal_years() < MIN_NORMAL_YEARS:
        out["reason"] = (f"Climate normals unavailable (need {MIN_NORMAL_YEARS} years of archive; "
                         f"have {data.normal_years()}). Run POST /api/climate/refresh when online.")
        return out
    if elapsed <= 0:
        out.update(available=True, equivalent_days=0.0, actual_gdd=0.0, normal_gdd=0.0, coverage=1.0,
                   days_by_source={}, missing_days=0)
        return out
    actual_total, normal_total, missing = 0.0, 0.0, 0
    by_source: dict[str, int] = defaultdict(int)
    for i in range(elapsed):
        d = start + timedelta(days=i)
        n = data.normal_gdd(d, params)
        if n is None:
            out["reason"] = f"No climate normal for {d:%d/%m}."
            return out
        normal_total += n
        t = data.actual(d)
        if t is None:
            missing += 1
            actual_total += n                         # missing day counted as a normal day
        else:
            by_source[t.kind] += 1
            actual_total += gdd(t.tmin, t.tmax, params)
    coverage = 1 - missing / elapsed
    horizon = [data.normal_gdd(norm_start + timedelta(days=i), params) or 0.0 for i in range(elapsed + 400)]
    eq = equivalent_normal_days(actual_total, horizon)
    reference_total = sum(horizon[:elapsed])
    out.update(actual_gdd=round(actual_total, 1), normal_gdd=round(normal_total, 1),
               reference_gdd=round(reference_total, 1), reference_start=norm_start.isoformat(),
               coverage=round(coverage, 3),
               days_by_source=dict(by_source), missing_days=missing,
               equivalent_days=round(eq, 2) if eq is not None else None)
    if coverage < MIN_COVERAGE:
        out["reason"] = (f"Only {coverage:.0%} of the days since {start:%d/%m/%Y} have a real temperature "
                         f"(minimum {MIN_COVERAGE:.0%}).")
        return out
    if eq is None:
        out["reason"] = "Degree-days out of the reference range."
        return out
    out["available"] = True
    return out


def date_when_actual_reaches(data: TemperatureData, params: ThermalParams, start: date, today: date,
                             target_gdd: float) -> date | None:
    """First date at whose end the actual degree-days since `start` reach `target_gdd` (past only)."""
    if target_gdd <= 0:
        return start
    acc = 0.0
    for i in range((today - start).days):
        d = start + timedelta(days=i)
        t = data.actual(d)
        acc += gdd(t.tmin, t.tmax, params) if t else (data.normal_gdd(d, params) or 0.0)
        if acc >= target_gdd:
            return d + timedelta(days=1)
    return None


def normal_gdd_between(data: TemperatureData, params: ThermalParams, start: date, n_days: float) -> float:
    """Normal degree-days over the first `n_days` (fractional) days from `start`."""
    total, i = 0.0, 0
    while i + 1 <= n_days:
        total += data.normal_gdd(start + timedelta(days=i), params) or 0.0
        i += 1
    frac = n_days - i
    if frac > 0:
        total += frac * (data.normal_gdd(start + timedelta(days=i), params) or 0.0)
    return total


def normal_days_to_accumulate(data: TemperatureData, params: ThermalParams, from_day: date, target_gdd: float) -> float | None:
    series = [data.normal_gdd(from_day + timedelta(days=i), params) or 0.0 for i in range(400)]
    return equivalent_normal_days(target_gdd, series)
