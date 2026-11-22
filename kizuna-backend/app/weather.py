"""Weather: Open-Meteo forecast when online, local-sensor fallback when offline.

Open-Meteo's `et0_fao_evapotranspiration` is FAO-56 Penman-Monteith ET0.
Offline, ET0 is computed with Hargreaves (FAO-56 Eq. 52) from the air
temperature sensor and rain is treated as unknown (0, flagged).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import httpx
from sqlmodel import Session, select

from .agronomy.et0 import et0_hargreaves, wind_speed_2m
from .config import settings
from .models import SensorReading, WeatherDay, utcnow

log = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
DAILY_VARS = [
    "et0_fao_evapotranspiration",
    "precipitation_sum",
    "precipitation_probability_max",
    "temperature_2m_max",
    "temperature_2m_min",
]
HOURLY_VARS = ["relative_humidity_2m", "wind_speed_10m"]


@dataclass
class WeatherInputs:
    day: date
    et0_mm: float
    et0_kind: str                  # forecast | calculated
    et0_source: str
    precip_mm: float
    precip_probability_pct: float | None
    rain_known: bool
    rh_min_pct: float | None
    u2_ms: float | None
    tmax_c: float | None
    tmin_c: float | None


def parse_open_meteo(payload: dict) -> list[WeatherDay]:
    daily = payload.get("daily", {})
    hourly = payload.get("hourly", {})
    rh_by_day: dict[str, list[float]] = defaultdict(list)
    wind_by_day: dict[str, list[float]] = defaultdict(list)
    for i, t in enumerate(hourly.get("time", [])):
        d = t[:10]
        rh = (hourly.get("relative_humidity_2m") or [None] * (i + 1))[i]
        ws = (hourly.get("wind_speed_10m") or [None] * (i + 1))[i]
        if rh is not None:
            rh_by_day[d].append(rh)
        if ws is not None:
            wind_by_day[d].append(ws)

    def col(name: str, i: int):
        values = daily.get(name)
        return values[i] if values and i < len(values) else None

    days = []
    for i, d in enumerate(daily.get("time", [])):
        winds = wind_by_day.get(d)
        u10 = sum(winds) / len(winds) if winds else None
        days.append(WeatherDay(
            day=date.fromisoformat(d),
            et0_mm=col("et0_fao_evapotranspiration", i),
            precip_mm=col("precipitation_sum", i),
            precip_probability_pct=col("precipitation_probability_max", i),
            tmax_c=col("temperature_2m_max", i),
            tmin_c=col("temperature_2m_min", i),
            rh_min_pct=min(rh_by_day[d]) if rh_by_day.get(d) else None,
            u2_ms=round(wind_speed_2m(u10, 10.0), 2) if u10 is not None else None,
            source="open-meteo",
        ))
    return days


def refresh_forecast(session: Session, client: httpx.Client | None = None) -> int:
    """Fetch and store the forecast. Returns the number of days stored (0 if offline)."""
    params = {
        "latitude": settings.farm_latitude,
        "longitude": settings.farm_longitude,
        "daily": ",".join(DAILY_VARS),
        "hourly": ",".join(HOURLY_VARS),
        "wind_speed_unit": "ms",
        "timezone": settings.timezone,
        "past_days": 1,
        "forecast_days": 7,          # 7 days: weekly irrigation plan
    }
    own_client = client is None
    client = client or httpx.Client(timeout=15)
    try:
        resp = client.get(OPEN_METEO_URL, params=params)
        resp.raise_for_status()
        days = parse_open_meteo(resp.json())
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Weather refresh failed (offline mode): %s", exc)
        return 0
    finally:
        if own_client:
            client.close()

    for wd in days:
        existing = session.exec(select(WeatherDay).where(WeatherDay.day == wd.day)).first()
        if existing:
            for field_name in ("et0_mm", "precip_mm", "precip_probability_pct", "tmax_c", "tmin_c",
                               "rh_min_pct", "u2_ms", "source"):
                setattr(existing, field_name, getattr(wd, field_name))
            existing.fetched_at = utcnow()
            session.add(existing)
        else:
            session.add(wd)
    session.commit()
    return len(days)


def _local_temperature_range(session: Session, now: datetime) -> tuple[float, float] | None:
    since = now - timedelta(hours=24)
    rows = session.exec(
        select(SensorReading.air_temp_c)
        .where(SensorReading.ts >= since, SensorReading.air_temp_c.is_not(None), SensorReading.valid == True)  # noqa: E712
    ).all()
    temps = [t for t in rows if t is not None]
    if len(temps) < 2:
        return None
    return max(temps), min(temps)


def weather_for_day(session: Session, day: date, now: datetime) -> WeatherInputs | None:
    wd = session.exec(select(WeatherDay).where(WeatherDay.day == day)).first()
    if wd and wd.et0_mm is not None and (now - wd.fetched_at) <= timedelta(hours=24):
        return WeatherInputs(
            day=day, et0_mm=wd.et0_mm, et0_kind="forecast", et0_source="Open-Meteo (FAO-56 Penman-Monteith)",
            precip_mm=wd.precip_mm or 0.0, precip_probability_pct=wd.precip_probability_pct, rain_known=True,
            rh_min_pct=wd.rh_min_pct, u2_ms=wd.u2_ms, tmax_c=wd.tmax_c, tmin_c=wd.tmin_c,
        )
    rng = _local_temperature_range(session, now)
    if rng is None:
        return None
    tmax, tmin = rng
    et0 = et0_hargreaves(tmax=tmax, tmin=tmin, latitude_deg=settings.farm_latitude,
                         day_of_year=day.timetuple().tm_yday)
    return WeatherInputs(
        day=day, et0_mm=round(et0, 2), et0_kind="calculated",
        et0_source="Hargreaves (FAO-56 Eq. 52) from local air temperature - offline fallback",
        precip_mm=0.0, precip_probability_pct=None, rain_known=False,
        rh_min_pct=None, u2_ms=None, tmax_c=tmax, tmin_c=tmin,
    )


def rain_next_24h(session: Session, day: date) -> WeatherDay | None:
    return session.exec(select(WeatherDay).where(WeatherDay.day == day + timedelta(days=1))).first()
