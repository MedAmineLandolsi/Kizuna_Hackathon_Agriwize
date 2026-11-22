"""Runtime settings, read from environment variables (or a .env file next to the app)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path


def _load_dotenv() -> None:
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = re.sub(r"\s+#.*$", "", value).strip()      # allow "KEY=value   # comment"
        os.environ.setdefault(key.strip(), value)


_load_dotenv()


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _parse_windows(raw: str) -> list[tuple[time, time]]:
    windows = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        start, end = part.split("-")
        windows.append((time.fromisoformat(start.strip()), time.fromisoformat(end.strip())))
    return windows


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "sqlite:///./kizuna.db"))

    # Farm location (used for ET0 and weather). Default: Tunis area - set your own.
    farm_latitude: float = field(default_factory=lambda: float(_env("FARM_LATITUDE", "36.81")))
    farm_longitude: float = field(default_factory=lambda: float(_env("FARM_LONGITUDE", "10.18")))
    farm_altitude_m: float = field(default_factory=lambda: float(_env("FARM_ALTITUDE_M", "10")))
    farm_coastal: bool = field(default_factory=lambda: _env("FARM_COASTAL", "true").lower() == "true")
    timezone: str = field(default_factory=lambda: _env("FARM_TIMEZONE", "Africa/Tunis"))

    # Decision rules (configurable operating choices, not agronomic constants)
    irrigation_windows: list[tuple[time, time]] = field(
        default_factory=lambda: _parse_windows(_env("IRRIGATION_WINDOWS", "05:00-09:00,18:00-21:00")))
    rain_postpone_probability_pct: float = field(
        default_factory=lambda: float(_env("RAIN_POSTPONE_PROBABILITY_PCT", "60")))
    reading_max_age_min: float = field(default_factory=lambda: float(_env("READING_MAX_AGE_MIN", "30")))
    min_tank_level_pct: float = field(default_factory=lambda: float(_env("MIN_TANK_LEVEL_PCT", "10")))
    max_volume_per_event_l: float = field(default_factory=lambda: float(_env("MAX_VOLUME_PER_EVENT_L", "500")))
    timeout_factor: float = field(default_factory=lambda: float(_env("TIMEOUT_FACTOR", "1.5")))

    # Post-irrigation verification
    delivery_tolerance: float = field(default_factory=lambda: float(_env("DELIVERY_TOLERANCE", "0.10")))
    moisture_check_delay_min: float = field(default_factory=lambda: float(_env("MOISTURE_CHECK_DELAY_MIN", "30")))
    moisture_response_min_ratio: float = field(
        default_factory=lambda: float(_env("MOISTURE_RESPONSE_MIN_RATIO", "0.5")))
    leak_flow_threshold_lpm: float = field(default_factory=lambda: float(_env("LEAK_FLOW_THRESHOLD_LPM", "0.5")))

    # AI plant identification
    ai_confidence_threshold: float = field(default_factory=lambda: float(_env("AI_CONFIDENCE_THRESHOLD", "0.80")))
    # Vision alerts (operating choices; set them with your extension officer)
    pest_alert_confidence: float = field(default_factory=lambda: float(_env("PEST_ALERT_CONFIDENCE", "0.60")))
    # address of the vision service as seen from the browser (photos and analysis on the camera page)
    vision_public_url: str = field(default_factory=lambda: _env("VISION_PUBLIC_URL", "http://localhost:8100"))
    animal_alert_confidence: float = field(default_factory=lambda: float(_env("ANIMAL_ALERT_CONFIDENCE", "0.50")))
    trap_alert_new_insects: int = field(default_factory=lambda: int(_env("TRAP_ALERT_NEW_INSECTS", "10")))
    rain_visual_min_confidence: float = field(default_factory=lambda: float(_env("RAIN_VISUAL_MIN_CONFIDENCE", "0.80")))
    rain_observation_max_age_min: float = field(default_factory=lambda: float(_env("RAIN_OBSERVATION_MAX_AGE_MIN", "60")))
    rain_alert_min_mm: float = field(default_factory=lambda: float(_env("RAIN_ALERT_MIN_MM", "1.0")))

    # Background scheduler
    scheduler_enabled: bool = field(default_factory=lambda: _env("SCHEDULER_ENABLED", "true").lower() == "true")
    decision_interval_min: float = field(default_factory=lambda: float(_env("DECISION_INTERVAL_MIN", "15")))
    weather_interval_min: float = field(default_factory=lambda: float(_env("WEATHER_INTERVAL_MIN", "180")))

    # Shared secret for the ESP32 (header X-Device-Key). Empty = no check (development only).
    device_api_key: str = field(default_factory=lambda: _env("DEVICE_API_KEY", ""))


settings = Settings()
