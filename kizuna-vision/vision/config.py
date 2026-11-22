"""Vision service settings (environment variables or a .env file next to the package)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

def load_env_file(path: Path) -> dict[str, str]:
    """Read KEY=value lines; blank lines, full-line and inline comments ("KEY=value  # note") are ignored."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = re.sub(r"\s+#.*$", "", v).strip()
    return values


for _k, _v in load_env_file(Path(__file__).resolve().parent.parent / ".env").items():
    os.environ.setdefault(_k, _v)


def _e(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    backend_url: str = field(default_factory=lambda: _e("BACKEND_URL", "http://localhost:8000"))
    device_key: str = field(default_factory=lambda: _e("DEVICE_API_KEY", ""))
    image_dir: str = field(default_factory=lambda: _e("IMAGE_DIR", "./images"))

    # Zero-shot model (prototype engine until fine-tuned models exist)
    use_clip: bool = field(default_factory=lambda: _e("USE_CLIP", "true").lower() == "true")
    clip_model: str = field(default_factory=lambda: _e("CLIP_MODEL", "ViT-B-32"))
    clip_pretrained: str = field(default_factory=lambda: _e("CLIP_PRETRAINED", "laion2b_s34b_b79k"))

    # YOLO detectors
    animal_model: str = field(default_factory=lambda: _e("ANIMAL_MODEL", "yolov8n.pt"))   # COCO, downloads itself
    pest_model: str = field(default_factory=lambda: _e("PEST_MODEL", ""))                 # your trained pest model
    crop_model: str = field(default_factory=lambda: _e("CROP_MODEL", ""))                 # your trained crop/stage classifier

    # Detection thresholds (the backend applies its own alert thresholds on top)
    detect_min_conf: float = field(default_factory=lambda: float(_e("DETECT_MIN_CONF", "0.35")))
    crop_min_conf: float = field(default_factory=lambda: float(_e("CROP_MIN_CONF", "0.50")))     # below: "unknown"
    crop_min_margin: float = field(default_factory=lambda: float(_e("CROP_MIN_MARGIN", "0.15")))  # top1 - top2
    rain_min_conf: float = field(default_factory=lambda: float(_e("RAIN_MIN_CONF", "0.70")))
    motion_min_changed_pct: float = field(default_factory=lambda: float(_e("MOTION_MIN_CHANGED_PCT", "2.0")))


settings = Settings()
