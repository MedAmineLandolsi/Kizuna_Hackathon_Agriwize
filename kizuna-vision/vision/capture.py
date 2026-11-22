"""Camera loop for the prototype: take a photo every N minutes and send it to the vision service.

Works with a USB webcam / laptop camera (index 0) or an IP/ESP32-CAM stream URL.

    python -m vision.capture --camera 0 --zone 1 --source mast --every 60
    python -m vision.capture --camera http://192.168.1.50:81/stream --zone 2 --source carriage --plant Z2-P03
    python -m vision.capture --image photo.jpg --zone 1 --source phone      # one file, once
"""
from __future__ import annotations

import argparse
import json
import time

import cv2
import httpx


def send(service: str, jpg: bytes, args) -> dict:
    data = {"source": args.source}
    if args.zone is not None:
        data["zone_id"] = str(args.zone)
    if args.plant:
        data["plant_ref"] = args.plant
    r = httpx.post(f"{service}/analyze", files={"image": ("frame.jpg", jpg, "image/jpeg")}, data=data, timeout=120)
    r.raise_for_status()
    return r.json()


def summary(res: dict) -> str:
    o = res["observation"]
    parts = []
    if o.get("crop"):
        parts.append(f"crop {o['crop']['key']} ({o['crop']['confidence']:.2f})")
    if o.get("stage"):
        parts.append(f"stage {o['stage']['key']}")
    if o.get("canopy"):
        parts.append(f"canopy {o['canopy']['canopy_cover_pct']}%")
    if o.get("trap"):
        parts.append(f"trap insects {o['trap'].get('insect_count')}")
    parts += [f"pest {p['label']} ({p['confidence']:.2f}, {p.get('engine', 'detector')})" for p in o.get("pests", [])]
    if o.get("health") and o["health"].get("hint") != "healthy":
        parts.append(o["health"]["message_en"])
    parts += [f"animal {a['label']} ({a['confidence']:.2f})" for a in o.get("animals", [])]
    if (o.get("rain") or {}).get("raining_now"):
        parts.append("RAIN")
    backend = res.get("backend") or {}
    alerts = backend.get("alerts") or []
    parts += [f"ALERT {a['kind']}" for a in alerts if a.get("new", True)]
    lines = [" | ".join(parts) or json.dumps(o)[:200]]
    lines += farmer_lines(backend)
    return "\n".join(lines)


def farmer_lines(backend: dict) -> list[str]:
    """Stage, next stage and irrigation plan, as returned by the backend for this photo."""
    out = []
    st = backend.get("stage") or {}
    if st.get("available"):
        out.append(f"  Stage: {st['stage']['name_en']} (since {st.get('stage_start_date')}, "
                   f"certainty {st['certainty']['level']})")
        if st.get("next_stage"):
            out.append(f"  Next stage: {st['next_stage']['name_en']} around {st.get('next_stage_date')} "
                       f"(in {st.get('days_to_next_stage')} days, estimate)")
        for r in st.get("reasons") or []:
            out.append(f"  To check: {r}")
    irr = backend.get("irrigation") or {}
    if irr.get("frequency"):
        f = irr["frequency"]
        out.append(f"  Irrigation: {f['text_en']} (~{irr['irrigations_per_week']} per week), "
                   f"~{irr['volume_per_irrigation_l']} L / {irr['duration_per_irrigation_min']} min each, "
                   f"~{irr['weekly_water_l']} L per week")
        n = irr.get("next_irrigation")
        out.append(f"  Next irrigation: {n['when_en']} ({n['volume_l']} L)" if n else "  Next irrigation: not set")
        out.append(f"  Status: {irr['status_text_en']}")
    for m in irr.get("missing") or []:
        out.append(f"  Missing: {m}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--service", default="http://localhost:8100")
    ap.add_argument("--camera", default="0", help="camera index or stream URL")
    ap.add_argument("--image", help="analyse one image file and exit")
    ap.add_argument("--zone", type=int)
    ap.add_argument("--plant")
    ap.add_argument("--source", default="mast", choices=["mast", "carriage", "phone", "trap"])
    ap.add_argument("--every", type=float, default=60, help="minutes between photos")
    args = ap.parse_args()

    if args.image:
        with open(args.image, "rb") as f:
            print(summary(send(args.service, f.read(), args)))
        return

    cam = cv2.VideoCapture(int(args.camera) if args.camera.isdigit() else args.camera)
    if not cam.isOpened():
        raise SystemExit(f"cannot open camera {args.camera}")
    print("Capturing. Ctrl+C to stop.")
    while True:
        for _ in range(5):          # let auto-exposure settle
            ok, frame = cam.read()
        if ok:
            jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()
            try:
                print(time.strftime("%H:%M"), summary(send(args.service, jpg, args)))
            except httpx.HTTPError as exc:
                print("vision service unreachable:", exc)
        time.sleep(args.every * 60)


if __name__ == "__main__":
    main()
