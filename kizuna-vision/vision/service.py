"""KIZUNA vision service (part 3 of 4).

One HTTP endpoint for every image source of the package options:
  mast camera / rail carriage / farmer's phone / sticky-trap camera.

Run:  uvicorn vision.service:app --port 8100      Docs: http://localhost:8100/docs
"""
from __future__ import annotations

import re

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import cv2
import httpx
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

from .config import settings
from .pipeline import SOURCES, Analyzer

log = logging.getLogger("kizuna.vision")
app = FastAPI(title="KIZUNA Vision", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

analyzer = Analyzer(settings)
_last_frames: dict[tuple, np.ndarray] = {}   # previous image per (zone, source, plant) for motion detection


def post_to_backend(obs: dict, client: httpx.Client | None = None) -> dict[str, Any]:
    if not settings.backend_url:
        return {"skipped": "BACKEND_URL not set"}
    headers = {"X-Device-Key": settings.device_key} if settings.device_key else {}
    own = client is None
    client = client or httpx.Client(timeout=15)
    try:
        r = client.post(f"{settings.backend_url}/api/vision/observations", json=obs, headers=headers)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as exc:
        return {"error": f"backend unreachable or refused: {exc}"}
    finally:
        if own:
            client.close()


def decode(data: bytes) -> np.ndarray:
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(422, "not a readable image (JPEG/PNG expected)")
    h, w = img.shape[:2]
    if max(h, w) > 1600:                         # keep analysis fast on a laptop / Raspberry Pi
        f = 1600 / max(h, w)
        img = cv2.resize(img, (int(w * f), int(h * f)), interpolation=cv2.INTER_AREA)
    return img


def save_image(data: bytes, zone_id: Optional[int], source: str) -> str:
    folder = Path(settings.image_dir)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S%f}_z{zone_id or 0}_{source}.jpg"
    (folder / name).write_bytes(data)
    return name


_IMAGE_NAME = re.compile(r"^[0-9A-Za-z_\-]+\.jpg$")


@app.get("/images/{name}")
def image(name: str) -> FileResponse:
    """Saved photos, for the dashboard's camera page (file names only, no paths)."""
    if not _IMAGE_NAME.match(name):
        raise HTTPException(404, "not found")
    path = Path(settings.image_dir) / name
    if not path.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "backend_url": settings.backend_url, "engines": analyzer.engines_status()}


@app.post("/analyze")
async def analyze(
    image: UploadFile = File(...),
    zone_id: Optional[int] = Form(None),
    source: str = Form("mast"),
    plant_ref: Optional[str] = Form(None),
    rain_sensor_wet: Optional[bool] = Form(None),
    trap_replaced: bool = Form(False),
    send: bool = Form(True),
) -> dict[str, Any]:
    if source not in SOURCES:
        raise HTTPException(422, f"source must be one of {SOURCES}")
    data = await image.read()
    img = decode(data)
    ref = save_image(data, zone_id, source)
    key = (zone_id, source, plant_ref)
    obs = analyzer.analyze(img, source=source, zone_id=zone_id, plant_ref=plant_ref, image_ref=ref,
                           prev_frame=_last_frames.get(key), rain_sensor_wet=rain_sensor_wet)
    _last_frames[key] = img
    if source == "trap" and obs.get("trap"):
        obs["trap"]["trap_replaced"] = trap_replaced     # restart the count after a new trap
    result = {"observation": obs}
    if send:
        result["backend"] = post_to_backend(obs)
    return result


WEB_PAGE = Path(__file__).resolve().parent / "web" / "index.html"


@app.get("/", response_class=HTMLResponse)
def analyzer_page() -> FileResponse:
    """The analyzer's own page: what each module does and a photo test (same style as the dashboard)."""
    return FileResponse(WEB_PAGE, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})
