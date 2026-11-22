"""Database engine, session and first-run seeding."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterator

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine, select

from .agronomy.reference_data import CROPS
from .config import settings
from .models import CropProfile, SystemState, Zone

_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=_connect_args)


def _add_missing_columns() -> None:
    """Tiny migration: add columns introduced by newer versions to an existing SQLite database."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in existing:
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" '
                                      f'{col.type.compile(engine.dialect)}'))


def init_db(seed_demo: bool = True) -> None:
    SQLModel.metadata.create_all(engine)
    _add_missing_columns()
    with Session(engine) as session:
        if session.get(SystemState, 1) is None:
            session.add(SystemState(id=1))
        for ref in CROPS.values():
            exists = session.exec(select(CropProfile).where(CropProfile.key == ref.key)).first()
            if not exists:
                session.add(CropProfile(
                    key=ref.key, name=ref.name, kc_ini=ref.kc_ini, kc_mid=ref.kc_mid, kc_end=ref.kc_end,
                    max_height_m=ref.max_height_m, l_ini=ref.l_ini, l_dev=ref.l_dev, l_mid=ref.l_mid,
                    l_late=ref.l_late, zr_ini_m=ref.zr_ini_m, zr_max_m=ref.zr_max_m, p=ref.p,
                    season_label=ref.season_label, source=ref.source, notes=list(ref.notes),
                ))
        session.commit()
        if seed_demo and not session.exec(select(Zone)).first():
            tomato = session.exec(select(CropProfile).where(CropProfile.key == "tomato")).one()
            pepper = session.exec(select(CropProfile).where(CropProfile.key == "pepper")).one()
            today = date.today()
            # Prototype: two independent zones, two crops (edit through the API/dashboard)
            session.add(Zone(
                name="Zone 1 - Tomato", valve_channel=0, crop_profile_id=tomato.id,
                planting_date=today - timedelta(days=50), soil_type="loam", area_m2=10.0,
                irrigation_method="drip", flow_rate_lpm=4.0,
                sensor_raw_dry=3000, sensor_theta_dry=0.05, sensor_raw_wet=1300, sensor_theta_wet=0.40,
                identification_status="confirmed",
            ))
            session.add(Zone(
                name="Zone 2 - Pepper", valve_channel=1, crop_profile_id=pepper.id,
                planting_date=today - timedelta(days=20), soil_type="sandy_loam", area_m2=10.0,
                irrigation_method="drip", flow_rate_lpm=4.0,
                sensor_raw_dry=3000, sensor_theta_dry=0.05, sensor_raw_wet=1300, sensor_theta_wet=0.40,
                identification_status="confirmed",
            ))
            session.commit()


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
