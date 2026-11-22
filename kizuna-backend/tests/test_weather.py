"""Open-Meteo parsing and storage (network mocked)."""
from datetime import date

import httpx
from sqlmodel import Session, SQLModel, select

from app.db import engine, init_db
from app.models import WeatherDay
from app.weather import parse_open_meteo, refresh_forecast

PAYLOAD = {
    "daily": {
        "time": ["2026-07-15", "2026-07-16"],
        "et0_fao_evapotranspiration": [6.4, 5.1],
        "precipitation_sum": [0.0, 12.5],
        "precipitation_probability_max": [3, 85],
        "temperature_2m_max": [34.0, 29.0],
        "temperature_2m_min": [22.0, 20.0],
    },
    "hourly": {
        "time": ["2026-07-15T00:00", "2026-07-15T12:00", "2026-07-16T00:00", "2026-07-16T12:00"],
        "relative_humidity_2m": [70, 32, 90, 60],
        "wind_speed_10m": [3.0, 5.0, 2.0, 4.0],
    },
}


def test_parse_open_meteo():
    days = parse_open_meteo(PAYLOAD)
    assert [d.day for d in days] == [date(2026, 7, 15), date(2026, 7, 16)]
    assert days[0].et0_mm == 6.4 and days[0].rh_min_pct == 32
    assert days[0].u2_ms == round(4.0 * 4.87 / __import__("math").log(67.8 * 10 - 5.42), 2)  # Eq. 47
    assert days[1].precip_mm == 12.5 and days[1].precip_probability_pct == 85


def test_refresh_forecast_stores_and_updates():
    SQLModel.metadata.drop_all(engine)
    init_db(seed_demo=False)
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=PAYLOAD))
    with Session(engine) as s, httpx.Client(transport=transport) as c:
        assert refresh_forecast(s, client=c) == 2
        assert refresh_forecast(s, client=c) == 2  # update, not duplicate
        assert len(s.exec(select(WeatherDay)).all()) == 2


def test_refresh_forecast_offline_returns_zero():
    def fail(req):
        raise httpx.ConnectError("offline")
    with Session(engine) as s, httpx.Client(transport=httpx.MockTransport(fail)) as c:
        assert refresh_forecast(s, client=c) == 0
